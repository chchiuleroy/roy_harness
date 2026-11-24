"""Regression tests for what the independent review of S1 found (小o, 2026-11-16). Each test names the failure: the protocol let a crash-recovered
commit bypass the current privacy rules, the manifest promised more than was checked, approvals never reached the display rule, hidden sources
leaked through relations, example links became graph edges, an ambiguous rename picked a source at random, and a few boundaries were off."""
import json
import os
import sqlite3
import unittest

from kb import commit, conv_csv, inbox, pathsafe, pipeline, relations
from kb.conv_csv import CsvConverter
from kb.conv_markdown import MarkdownConverter
from kb.errors import CommitFailed
from kb.profile import Profile

from .support import approved_proposal, run_py
from .world import World


def hide(world, source_id):
    approved_proposal(world.conn, f"pr_hide_{source_id[4:10]}", diff=json.dumps({"target_id": source_id, "level": "a"}))
    world.conn.execute("INSERT INTO tombstones(tombstone_id,target_id,level,proposal_id,at) VALUES (?,?, 'a',?,'t')",
                       ("tomb_" + source_id[4:12], source_id, f"pr_hide_{source_id[4:10]}"))


class TestRecoveryUsesCurrentRules(World):
    def kill_after_journal(self):
        code = (f"from kb import commit,db,pipeline,cas;from kb.home import Home;h=Home({self.home.path!r});c=db.connect(h.db_path);"
                f"pipeline.process_pending(c,cas.BlobStore(h.sub('blobs')));commit.build(h,c)")
        self.assertEqual(run_py(code, {"KB_FAULT": "commit.after_journal"}).returncode, 137)

    def test_a_profile_change_during_the_crash_stops_recovery_from_activating_the_new_commit(self):
        """A publishing commit was assembled under the OLD rules. If the rules changed before restart, finishing it would publish text the
        current policy may forbid; recovery must verify like a fresh build and abandon it."""
        self.seed_docs()
        old = self.build()
        self.add("alpha.md", "# Alpha\n\nnew text about owls\n")
        self.kill_after_journal()
        self.conn.execute("UPDATE collections SET profile_json=? WHERE collection_id=?",
                          (Profile(sensitivity="secret", index_plaintext=False).to_json(), self.cid))
        actions = commit.recover(self.home, self.conn)
        self.assertEqual([a for _, a in actions], ["abandoned"])
        self.assertEqual(commit.active_commit_id(self.conn), old.commit_id)
        self.assertEqual(sorted(self.states().values()), ["abandoned", "active"])

    def test_a_tombstone_during_the_crash_stops_recovery_the_same_way(self):
        self.seed_docs()
        old = self.build()
        self.add("alpha.md", "# Alpha\n\nnew text about owls\n")
        self.kill_after_journal()
        hide(self, self.b.source_id)
        actions = commit.recover(self.home, self.conn)
        self.assertEqual([a for _, a in actions], ["abandoned"])
        self.assertEqual(commit.active_commit_id(self.conn), old.commit_id)

    def test_recovery_does_not_move_a_commit_that_fails_the_current_rules_into_place(self):
        """Activation would refuse it anyway, but the check belongs BEFORE the rename: a rejected commit must never appear among the published
        segments, even for a moment (members are frozen by triggers once staged, so the rules are the thing that can change)."""
        self.seed_docs()
        self.build()
        self.add("alpha.md", "# Alpha\n\nnew text about owls\n")
        self.kill_after_journal()
        self.conn.execute("UPDATE collections SET profile_json=? WHERE collection_id=?",
                          (Profile(sensitivity="secret", index_plaintext=False).to_json(), self.cid))
        renames = []
        real = commit._publish_rename
        commit._publish_rename = lambda a, b: (renames.append(b), real(a, b))[1]
        self.addCleanup(setattr, commit, "_publish_rename", real)
        commit.recover(self.home, self.conn)
        self.assertEqual(renames, [])

    def test_a_change_after_the_rename_but_before_activation_is_caught_by_the_activation_itself(self):
        self.seed_docs()
        old = self.build()
        self.add("alpha.md", "# Alpha\n\nnew text about owls\n")
        real = commit._publish_rename

        def rename_then_change(src, dst):
            real(src, dst)
            self.conn.execute("UPDATE collections SET profile_json=? WHERE collection_id=?", (Profile(agent_visible=True).to_json(), self.cid))
        commit._publish_rename = rename_then_change
        self.addCleanup(setattr, commit, "_publish_rename", real)
        with self.assertRaises(CommitFailed) as cm:
            self.build()
        self.assertIn("changed after this commit was assembled", str(cm.exception))
        self.assertEqual(commit.active_commit_id(self.conn), old.commit_id)
        self.assertIn(self.a.source_id, self.hit_sources("falcon"))


class TestManifestPromisesAreChecked(World):
    """Each tamper happens to the database while the commit is still being staged; the file hashes all stay consistent, so only the membership
    comparison and the recomputed commitment can notice."""

    def attempt(self, tamper, expect):
        self.seed_docs()
        old = self.build()
        self.add("alpha.md", "# Alpha\n\nchange so that a new commit is needed\n")
        real = commit._stage_members

        def stage_then_tamper(conn, commit_id, asm):
            real(conn, commit_id, asm)
            tamper(conn, commit_id)
        commit._stage_members = stage_then_tamper
        self.addCleanup(setattr, commit, "_stage_members", real)
        with self.assertRaises(CommitFailed) as cm:
            self.build()
        self.assertIn(expect, str(cm.exception))
        self.assertEqual(commit.active_commit_id(self.conn), old.commit_id)
        self.assertEqual(sorted(self.states().values()), ["abandoned", "active"])

    def test_an_extra_unit_version_in_the_database(self):
        def extra(conn, cid):
            conn.execute("INSERT INTO units VALUES ('unit_bogusbogusbogusbogus','t')")
            conn.execute("INSERT INTO unit_versions VALUES ('uv_bogusbogusbogusbogusb','unit_bogusbogusbogusbogus',NULL,'p','{}','f')")
            conn.execute("INSERT INTO commit_units VALUES (?, 'uv_bogusbogusbogusbogusb')", (cid,))
        self.attempt(extra, "unit versions differ")

    def test_a_unit_version_missing_from_the_database(self):
        self.attempt(lambda c, cid: c.execute("DELETE FROM commit_units WHERE rowid = (SELECT min(rowid) FROM commit_units WHERE commit_id = ?)", (cid,)),
                     "unit versions differ")

    def test_a_display_role_changed_in_the_database(self):
        self.attempt(lambda c, cid: c.execute("UPDATE commit_statements SET role='alternate' WHERE rowid = "
                                              "(SELECT min(rowid) FROM commit_statements WHERE commit_id = ? AND role='display')", (cid,)),
                     "display roles differ")

    def test_a_commitment_that_the_members_do_not_reproduce(self):
        """The manifest file is self-consistent (its own hash is recorded), so only recomputing the commitment from what the database holds can
        tell that the promise written into it was wrong."""
        self.seed_docs()
        old = self.build()
        self.add("alpha.md", "# Alpha\n\nchange so that a new commit is needed\n")
        real = commit.assemble

        def wrong(*a, **k):
            asm = real(*a, **k)
            asm.members_sha256 = "0" * 64
            return asm
        commit.assemble = wrong
        self.addCleanup(setattr, commit, "assemble", real)
        with self.assertRaises(CommitFailed) as cm:
            self.build()
        self.assertIn("members commitment differs", str(cm.exception))
        self.assertEqual(commit.active_commit_id(self.conn), old.commit_id)

    def test_a_chunk_whose_members_do_not_reproduce_its_id(self):
        self.attempt(lambda c, cid: c.execute("UPDATE chunks SET unit_version_ids_json='[\"uv_zzzzzzzzzzzzzzzzzzzz\"]' WHERE chunk_id = "
                                              "(SELECT chunk_id FROM commit_chunks WHERE commit_id = ? LIMIT 1)", (cid,)),
                     "chunk payload does not match its id")


class TestApprovalsReachTheDisplayRule(World):
    def title_shown(self, source_id):
        r = self.build()
        rows = self.conn.execute("SELECT json_extract(s.value_json,'$'), s.origin FROM statements s JOIN commit_statements cs ON cs.statement_id=s.statement_id "
                                 "WHERE cs.commit_id=? AND s.subject_id=? AND s.key='title' AND cs.role='display'", (r.commit_id, source_id)).fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def statement(self, sid, subject, value, origin, proposal=None, state="approved", override=0, supersedes=None):
        kind = "ai_edit" if origin == "ai" else "metadata"                      # a statement's approval must be of a kind that fits its origin
        if proposal and state == "approved":
            approved_proposal(self.conn, proposal, kind=kind, override=override)
        elif proposal:
            self.conn.execute("INSERT OR IGNORE INTO proposals(proposal_id,kind,diff_json,requested_by,state,created_at) VALUES (?,?,?,?,?,?)",
                              (proposal, kind, "{}", "roy", state, "t"))
        self.conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,supersedes,state,proposal_id) "
                          "VALUES (?,?,?,?,?, ?, ?, 'candidate', ?)",
                          (sid, subject, "title", json.dumps(value), origin, json.dumps({"proposal_id": proposal} if proposal else {}), supersedes, proposal))

    def setUp(self):
        super().setUp()
        self.p = self.add("page.md", "---\ntitle: Native Title\n---\nbody text")
        pipeline.process_pending(self.conn, self.store)

    def test_a_native_title_is_shown_when_nothing_else_is_approved(self):
        self.assertEqual(self.title_shown(self.p.source_id), ("Native Title", "native"))

    def test_an_approved_human_statement_is_shown_and_an_unapproved_one_is_not(self):
        """Gate 1 decision 6: only what the user approved becomes the shown value; a pending proposal changes nothing."""
        self.statement("st_pending", self.p.source_id, "Pending Title", "human", "pr_pending", state="pending")
        self.assertEqual(self.title_shown(self.p.source_id), ("Native Title", "native"))
        self.statement("st_human", self.p.source_id, "Human Title", "human", "pr_human")
        self.assertEqual(self.title_shown(self.p.source_id), ("Human Title", "human"))

    def test_ai_cannot_replace_a_human_statement_unless_the_user_agreed_to_override(self):
        self.statement("st_human", self.p.source_id, "Human Title", "human", "pr_human")
        self.statement("st_ai", self.p.source_id, "AI Title", "ai", "pr_ai", supersedes="st_human")
        self.assertEqual(self.title_shown(self.p.source_id), ("Human Title", "human"))
        # approvals are never rewritten, so the user's agreement to override is a new proposal with its own approval
        self.statement("st_ai2", self.p.source_id, "AI Title 2", "ai", "pr_ai2", override=1, supersedes="st_human")
        self.assertEqual(self.title_shown(self.p.source_id), ("AI Title 2", "ai"))


class TestRelationsAndHiddenSources(World):
    def setUp(self):
        super().setUp()
        self.one_ = self.add("one.md", "# One\n\nsee [[two]]\n")
        self.two = self.add("two.md", "# Two\n\ntext\n")
        pipeline.process_pending(self.conn, self.store)
        relations.derive(self.conn)

    def edges(self):
        return [r for r in self.conn.execute("SELECT s.statement_id FROM statements s JOIN commit_statements cs ON cs.statement_id=s.statement_id "
                                             "WHERE cs.commit_id=(SELECT commit_id FROM active_commit WHERE id=1) AND s.key='links_to_source'")]

    def test_an_edge_to_a_hidden_source_is_not_committed_even_before_the_next_derive(self):
        """Hiding a source must remove it from the graph at once: ids and names of hidden sources must not leak through relations."""
        self.build()
        self.assertEqual(len(self.edges()), 1)
        hide(self, self.two.source_id)
        self.build()
        self.assertEqual(self.edges(), [])
        self.assertEqual(relations.neighbors(self.conn, self.one_.source_id), ([], []))

    def test_derive_rejects_the_edge_once_its_target_is_hidden(self):
        hide(self, self.two.source_id)
        counts = relations.derive(self.conn)
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM statements WHERE key='links_to_source' AND state != 'rejected'").fetchone()[0], 0)

    def test_neighbors_hide_a_source_hidden_since_the_commit(self):
        self.build()
        self.assertEqual(relations.neighbors(self.conn, self.one_.source_id)[0], [self.two.source_id])
        hide(self, self.two.source_id)
        self.assertEqual(relations.neighbors(self.conn, self.one_.source_id), ([], []))


class TestWikilinkSources(unittest.TestCase):
    DOC = ("---\ntitle: T\nrelated: \"[[in front matter]]\"\n---\nreal [[body link]] here\n\n```\nexample [[in code]]\n```\n\n"
           "~~~\nalso [[tilde code]]\n~~~\n\nand [[second real]]\n")

    def test_only_links_in_prose_count(self):
        """Example text in a code block and metadata values are not statements about the knowledge; they must not become graph edges."""
        res = MarkdownConverter().convert(self.DOC.encode())
        self.assertEqual(res.meta["links"], ["body link", "second real"])


class TestAmbiguousRename(World):
    def test_when_several_sources_lost_their_files_a_moved_file_does_not_pick_one_at_random(self):
        p1, p2 = self.add("a/one.txt", "identical bytes for the rename test"), None
        p2 = self.add("b/two.txt", "identical bytes for the rename test")
        self.assertEqual(p2.outcome, "duplicate_blob")
        os.remove(os.path.join(self.src, "a", "one.txt"))
        os.remove(os.path.join(self.src, "b", "two.txt"))
        moved = self.add("c/three.txt", "identical bytes for the rename test")
        self.assertEqual((moved.outcome, moved.reason), ("new", "ambiguous_rename"))
        self.assertNotIn(moved.source_id, (p1.source_id, p2.source_id))
        self.assertEqual(self.one("SELECT count(*) FROM sources")[0], 3)


class TestIsInside(World):
    def test_inside_outside_and_the_shared_prefix_trap(self):
        a, ab = os.path.join(self.src, "a"), os.path.join(self.src, "a", "b")
        other = os.path.join(self.src, "a_other")                      # shares the text prefix "a" but is not inside it
        for d in (ab, other):
            os.makedirs(d)
        self.assertTrue(pathsafe.is_inside(a, ab))
        self.assertTrue(pathsafe.is_inside(a, a))
        self.assertFalse(pathsafe.is_inside(ab, a))
        self.assertFalse(pathsafe.is_inside(a, other))


class TestBoundariesAfterTheRead(World):
    def test_a_link_flag_appearing_after_the_read_drops_the_record(self):
        """Narrowing the check-then-open gap: the link check runs again after the bytes were read."""
        p = os.path.join(self.src, "late.txt")
        with open(p, "w") as f:
            f.write("content")
        real = pathsafe.entry_problem
        calls = []

        def late(path, name, is_dir):
            calls.append(1)
            return None if len(calls) == 1 else "reparse_point_not_followed"
        pathsafe.entry_problem = late
        self.addCleanup(setattr, pathsafe, "entry_problem", real)
        r = self.intake.add_file(p)
        self.assertEqual((r.outcome, r.reason), ("skipped", "reparse_point_not_followed"))
        self.assertEqual(self.one("SELECT count(*) FROM sources")[0], 0)

    def test_a_file_that_ends_up_outside_the_folder_after_the_read_is_dropped(self):
        folder = os.path.join(self.src, "scan")
        os.makedirs(folder)
        p = os.path.join(folder, "x.txt")
        with open(p, "w") as f:
            f.write("content")
        real = pathsafe.is_inside
        calls = []
        pathsafe.is_inside = lambda root, path: (calls.append(1), len(calls) == 1)[1]
        self.addCleanup(setattr, pathsafe, "is_inside", real)
        r = self.intake.add_file(p, boundary=folder)
        self.assertEqual((r.outcome, r.reason), ("skipped", "outside_the_folder"))

    def test_a_replaced_file_is_noticed_by_its_identity_not_only_by_size_and_time(self):
        p = os.path.join(self.src, "same.txt")
        with open(p, "w") as f:
            f.write("content")
        real = inbox._identity
        calls = []

        def changing(st):
            calls.append(1)
            return real(st) + (len(calls),)                    # same size and time, but the file is "not the same one" the second time
        inbox._identity = changing
        self.addCleanup(setattr, inbox, "_identity", real)
        r = self.intake.add_file(p)
        self.assertEqual((r.outcome, r.reason), ("skipped", "changed_while_reading"))


class TestCsvCapIsExact(unittest.TestCase):
    def test_exactly_the_cap_is_kept(self):
        old = conv_csv.MAX_ROWS
        conv_csv.MAX_ROWS = 5
        self.addCleanup(setattr, conv_csv, "MAX_ROWS", old)
        res = CsvConverter().convert(("h\n" + "\n".join(str(i) for i in range(50))).encode())
        self.assertEqual(res.report["rows"], 5)                                        # header plus four data rows, not six
        self.assertEqual(res.status, "partial")


if __name__ == "__main__":
    unittest.main()
