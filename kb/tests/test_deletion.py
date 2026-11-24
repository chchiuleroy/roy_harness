"""S2b: deletion levels a and b (T-D2 the tombstone takes effect, T-D3 where the removed text may and may not be left). Hiding is not erasing:
the original file stays (Gate 1 decision 4), and everything derived that could still make the text findable must go."""
import hashlib
import io
import json
import os
import sqlite3
import time
import unittest
from contextlib import redirect_stderr

from kb import cli, commit, ids, inventory, pipeline, relations, review
from kb import conv_base as cb
from kb.canonical import canonical_bytes
from kb.errors import KbError, ReviewRefused
from kb.search import Searcher

from .world import World

CANARY = "zqcanaryx91"


class TestHidingTakesEffect(World):
    def setUp(self):
        super().setUp()
        self.a = self.add("alpha.md", f"# Alpha\n\nThe falcon flies over the harbor, marker {CANARY}. See [[beta]].\n")
        self.b = self.add("beta.md", "# Beta\n\nKelp forests grow offshore. Back to [[alpha]].\n")
        self.build()

    def hide(self, target, level="a"):
        return self.decide("delete", {"target_id": target, "level": level})

    def test_it_takes_effect_at_the_next_commit_not_before(self):
        """Gate 2 4.10: approval creates the tombstone, the commit makes it real. Until then the published version is what it was."""
        self.hide(self.a.source_id)
        self.assertIn(self.a.source_id, self.hit_sources(CANARY))
        self.build()
        self.assertNotIn(self.a.source_id, self.hit_sources(CANARY))
        self.assertIn(self.b.source_id, self.hit_sources("kelp"))

    def test_a_hidden_source_leaves_every_read_path(self):
        self.hide(self.a.source_id)
        self.build()
        s = Searcher(self.home, self.conn)
        self.assertEqual([h.source_id for h in s.sources(CANARY, 10)], [])
        self.assertEqual([h.source_id for h in s.chunks(CANARY, 10)], [])
        self.assertEqual(relations.neighbors(self.conn, self.b.source_id), ([], []))
        self.assertFalse(any(self.a.source_id == sid for sid, _ in commit.included_sources(self.conn)))

    def test_the_original_file_and_the_records_stay(self):
        """Gate 1 decision 4: hiding at levels a and b keeps the evidence, so the decision can be revisited."""
        sha = self.one("SELECT blob_sha256 FROM revisions WHERE source_id=?", self.a.source_id)[0]
        self.hide(self.a.source_id, "b")
        self.build()
        self.assertTrue(self.store.verify(self.conn, sha))
        self.assertTrue(os.path.isfile(self.store.blob_path(sha)))
        self.assertEqual(self.one("SELECT count(*) FROM sources WHERE source_id=?", self.a.source_id)[0], 1)

    def test_hiding_one_unit_leaves_the_rest_of_the_source(self):
        unit_id = self.one("SELECT unit_id FROM unit_versions WHERE unit_version_id=?", self.uvs(self.a.source_id)[-1])[0]
        self.hide(unit_id)
        self.build()
        self.assertNotIn(self.a.source_id, self.hit_sources(CANARY))
        self.assertIn(self.a.source_id, self.hit_sources("alpha"))               # the heading unit is still there

    def test_hiding_can_be_raised_from_a_to_b_and_the_stricter_level_wins(self):
        """Level b includes level a: once both exist for a target, the target is treated as b (hidden from general access), whatever the order."""
        self.hide(self.a.source_id, "a")
        self.assertEqual(commit.tombstone_level(self.conn, self.a.source_id), "a")
        self.hide(self.a.source_id, "b")
        self.assertEqual(commit.tombstone_level(self.conn, self.a.source_id), "b")
        self.assertIsNone(commit.tombstone_level(self.conn, self.b.source_id))

    def test_the_target_must_exist_and_level_c_is_not_available(self):
        with self.assertRaises(ReviewRefused):
            review.request_delete(self.conn, "src_aaaaaaaaaaaaaaaaaaaaaaaa", "a", "roy")
        with self.assertRaises(ReviewRefused):
            review.request_delete(self.conn, self.a.source_id, "c", "roy")
        with self.assertRaises(ReviewRefused):
            review.request_delete(self.conn, self.a.source_id, "z", "roy")

    def test_a_delete_proposal_alone_hides_nothing(self):
        review.request_delete(self.conn, self.a.source_id, "a", "roy")
        self.assertEqual(self.one("SELECT count(*) FROM tombstones")[0], 0)

    def test_older_versions_that_still_hold_the_text_are_removed(self):
        """Gate 1 H2: a retired commit's index would keep the hidden text findable on disk even though rolling back to it would be refused."""
        old = commit.active_commit_id(self.conn)
        self.add("gamma.md", "# Gamma\n\nanother page\n")
        self.build()                                                          # now `old` is retired and still holds the canary
        self.assertTrue(os.path.isdir(commit.segment_dir(self.home, old)))
        self.hide(self.a.source_id)
        r = self.build()
        self.assertGreaterEqual(r.pruned, 2)
        self.assertFalse(os.path.isdir(commit.segment_dir(self.home, old)))
        from kb.errors import CommitFailed
        with self.assertRaises(CommitFailed):
            commit.rollback(self.home, self.conn)

    def test_pruning_leaves_history_alone_when_nothing_was_hidden(self):
        old = commit.active_commit_id(self.conn)
        self.add("gamma.md", "# Gamma\n\nanother page\n")
        r = self.build()
        self.assertEqual(r.pruned, 0)
        self.assertTrue(os.path.isdir(commit.segment_dir(self.home, old)))
        self.assertEqual(commit.rollback(self.home, self.conn), old)


class TestWhereTheTextMayRemain(World):
    """T-D3: scan every class that `kb inventory` lists, with a unique string, and compare with what hiding promises (inventory.FILE_EXPECTATION)."""

    def setUp(self):
        super().setUp()
        self.a = self.add("alpha.md", f"# Alpha\n\nmarker {CANARY} stays only in the original.\n")
        self.add("beta.md", "# Beta\n\nunrelated\n")
        self.build()
        self.decide("metadata", {"statements": [{"subject_id": self.a.source_id, "key": "note", "value": f"a note about {CANARY}"}]})
        self.build()

    def test_the_scan_sees_the_string_where_it_is_before_hiding(self):
        """A negative control: a scan that finds nothing anywhere would make the later 'absent' assertions meaningless."""
        found = inventory.scan(self.home, self.conn, CANARY)
        self.assertTrue(found["raw_blobs"])
        self.assertTrue(found["index_segments"])
        self.assertTrue(found["db_proposals"])
        self.assertTrue(found["db_derived_text"])
        self.assertTrue(found["db_file_bytes"])
        self.assertFalse(found["db_audit"])

    def hide_and_check(self, level):
        self.decide("delete", {"target_id": self.a.source_id, "level": level})
        self.build()
        self.assertEqual(inventory.check_after_hide(self.home, self.conn, CANARY), [])
        found = inventory.scan(self.home, self.conn, CANARY)
        self.assertTrue(found["raw_blobs"])                                 # kept on purpose
        for cls in ("index_segments", "staging", "logs", "db_audit", "db_proposals", "db_derived_text", "db_file_bytes"):
            self.assertFalse(found[cls], cls)                              # also the proposals (second review: they used to stay until their retention ended)

    def test_after_hiding_at_level_a(self):
        self.hide_and_check("a")

    def test_after_hiding_at_level_b(self):
        self.hide_and_check("b")

    def test_the_check_reports_a_class_that_breaks_the_promise(self):
        """If hiding left the text in the index (here: hiding without a commit), the check must say which class is wrong."""
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        bad = inventory.check_after_hide(self.home, self.conn, CANARY)
        self.assertEqual([b[0] for b in bad], ["index_segments", "db_derived_text", "db_proposals", "db_file_bytes"])     # nothing is cleaned before the commit


class TestCommandLine(World):
    def setUp(self):
        super().setUp()
        self.h = self.home.path
        self.p = os.path.join(self.src, "n")
        os.makedirs(self.p)
        for name, text in (("alpha.md", f"# Alpha\n\nfalcon {CANARY}\n"), ("beta.md", "# Beta\n\nkelp\n")):
            with open(os.path.join(self.p, name), "w", encoding="utf-8") as f:
                f.write(text)
        self.conn.close()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["--home", self.h, *argv], out=out)
        return code, out.getvalue(), err.getvalue()

    def test_delete_asks_first_and_hides_after_approval_and_commit(self):
        self.assertEqual(self.cli("add", self.p, "--settle", "0")[0], 0)
        self.cli("commit")
        code, out, _ = self.cli("delete", "alpha.md", "--by", "roy")
        self.assertEqual(code, 0)
        self.assertIn("nothing is hidden until it is approved", out)
        self.assertIn(CANARY, self.cli("search", CANARY)[1])
        pid = out.split("proposal ")[1].split()[0]
        self.assertIn("pending proposals: 1", self.cli("review", "list")[1])
        self.assertIn("alpha.md", self.cli("review", "show", pid)[1])         # the screen names the file, not just an identifier
        self.assertEqual(self.cli("review", "approve", pid, "--by", "roy")[0], 0)
        self.assertIn(CANARY, self.cli("search", CANARY)[1])                  # approved and applied, but not committed yet
        self.cli("commit")
        self.assertIn("no results", self.cli("search", CANARY)[1])
        self.assertIn("kelp", self.cli("search", "kelp")[1])

    def test_show_tells_level_a_from_level_b(self):
        self.cli("add", self.p, "--settle", "0")
        self.cli("commit")
        self.cli("delete", "alpha.md", "--by", "roy", "--yes")
        self.cli("delete", "beta.md", "--by", "roy", "--yes", "--level", "b")
        self.cli("commit")
        code, out, _ = self.cli("show", "alpha.md")
        self.assertEqual(code, 0)
        self.assertIn("HIDDEN at level a", out)
        code, out, err = self.cli("show", "beta.md")
        self.assertEqual(code, 2)
        self.assertIn("matches 0 sources", err)                                 # the same words as a name that never existed
        code, out, _ = self.cli("show", "beta.md", "--include-hidden")
        self.assertEqual(code, 0)
        self.assertIn("HIDDEN at level b", out)

    def test_show_does_not_print_the_text_of_a_unit_hidden_at_level_b(self):
        """Found in the S2 review: hiding a unit kept it out of the index, but `kb show` still listed its text."""
        self.cli("add", self.p, "--settle", "0")
        self.cli("commit")
        out = self.cli("show", "alpha.md")[1]
        self.assertIn(CANARY, out)
        import re
        uv = re.search(r"(uv_\w+)\s+'falcon", out).group(1)
        from kb import db
        c = db.connect(self.home.db_path, readonly=True)
        self.addCleanup(c.close)
        unit_id = c.execute("SELECT unit_id FROM unit_versions WHERE unit_version_id=?", (uv,)).fetchone()[0]
        self.cli("delete", unit_id, "--by", "roy", "--yes", "--level", "b")
        out = self.cli("show", "alpha.md")[1]
        self.assertNotIn(CANARY, out)
        self.assertIn("1 hidden", out)

    def test_show_still_lists_a_unit_hidden_only_at_level_a(self):
        self.cli("add", self.p, "--settle", "0")
        self.cli("commit")
        import re
        uv = re.search(r"(uv_\w+)\s+'falcon", self.cli("show", "alpha.md")[1]).group(1)
        from kb import db
        c = db.connect(self.home.db_path, readonly=True)
        self.addCleanup(c.close)
        unit_id = c.execute("SELECT unit_id FROM unit_versions WHERE unit_version_id=?", (uv,)).fetchone()[0]
        self.cli("delete", unit_id, "--by", "roy", "--yes", "--level", "a")
        self.assertIn(CANARY, self.cli("show", "alpha.md")[1])

    def test_links_says_the_same_as_for_a_name_that_never_existed(self):
        """Round 2 of the S2 review: `kb links` still printed the name and id of a source hidden at level b."""
        self.cli("add", self.p, "--settle", "0")
        self.cli("commit")
        self.assertEqual(self.cli("links", "alpha.md")[0], 0)
        self.cli("delete", "alpha.md", "--by", "roy", "--yes", "--level", "a")
        self.assertEqual(self.cli("links", "alpha.md")[0], 0)                          # level a stays reachable for the owner
        self.cli("delete", "alpha.md", "--by", "roy", "--yes", "--level", "b")
        code, out, err = self.cli("links", "alpha.md")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("matches 0 sources", err)

    def test_yes_hides_in_one_step_but_still_goes_through_a_proposal(self):
        self.cli("add", self.p, "--settle", "0")
        self.cli("commit")
        self.assertEqual(self.cli("delete", "alpha.md", "--by", "roy", "--yes")[0], 0)
        from kb import db
        c = db.connect(self.home.db_path, readonly=True)
        self.addCleanup(c.close)
        self.assertEqual(c.execute("SELECT p.kind, p.state FROM tombstones t JOIN proposals p ON p.proposal_id = t.proposal_id").fetchall(), [("delete", "applied")])


if __name__ == "__main__":
    unittest.main()


class TestErasure(World):
    """Gate 1 H2 (S4): once something is hidden, the text derived from it is erased from the database too, not only from the index."""
    SECRET = "zqerasex42"

    def setUp(self):
        super().setUp()
        self.a = self.add("alpha.md", f"# Alpha\n\nFirst paragraph with {self.SECRET} inside.\n\nSecond paragraph stays plain.\n")
        self.b = self.add("beta.md", "# Beta\n\nBeta keeps its words, see [[alpha]].\n")
        self.build()

    def rows_with(self, needle):
        return (self.one("SELECT count(*) FROM chunks WHERE instr(text, ?) > 0", needle)[0], self.one("SELECT count(*) FROM statements WHERE instr(value_json, ?) > 0", needle)[0])

    def test_before_hiding_the_text_is_in_the_database_a_negative_control(self):
        chunks, statements = self.rows_with(self.SECRET)
        self.assertGreater(chunks, 0)
        self.assertGreater(statements, 0)

    def test_after_hiding_a_source_its_text_is_gone_from_chunks_and_statements_and_the_other_source_is_untouched(self):
        keep_chunks = self.one("SELECT count(*) FROM chunks WHERE instr(text, 'Beta keeps its words') > 0")[0]
        self.decide("delete", {"target_id": self.a.source_id, "level": "b"})
        self.build()
        self.assertEqual(self.rows_with(self.SECRET), (0, 0))
        self.assertEqual(self.rows_with("Second paragraph"), (0, 0))
        self.assertEqual(self.one("SELECT count(*) FROM chunks WHERE instr(text, 'Beta keeps its words') > 0")[0], keep_chunks)
        self.assertGreater(self.one("SELECT count(*) FROM statements WHERE instr(value_json, 'Beta keeps its words') > 0")[0], 0)

    def test_erased_statements_keep_their_ids_and_their_place_only_the_content_goes(self):
        subject_rows = self.one("SELECT count(*) FROM statements WHERE subject_id = ?", self.a.source_id)[0]
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertEqual(self.one("SELECT count(*) FROM statements WHERE subject_id = ?", self.a.source_id)[0], subject_rows)
        values = {r[0] for r in self.conn.execute("SELECT value_json FROM statements WHERE subject_id = ?", (self.a.source_id,))}
        self.assertEqual(values, {commit.ERASED_VALUE})
        evidence = {r[0] for r in self.conn.execute("SELECT evidence_json FROM statements WHERE subject_id = ?", (self.a.source_id,))}
        self.assertEqual(evidence, {commit.ERASED_EVIDENCE})

    def test_hiding_one_unit_erases_that_unit_and_the_other_units_of_the_source_stay(self):
        first, second = self.uvs(self.a.source_id)[1:3]                       # the first paragraph (it holds the secret) and the second one
        unit = self.one("SELECT unit_id FROM unit_versions WHERE unit_version_id = ?", first)[0]
        self.decide("delete", {"target_id": unit, "level": "a"})
        self.build()
        self.assertEqual(self.rows_with(self.SECRET), (0, 0))
        survivors = [r[0] for r in self.conn.execute("SELECT value_json FROM statements WHERE subject_id = ? AND key = 'text'", (second,))]
        self.assertTrue(any("Second paragraph stays plain" in v for v in survivors))

    def test_a_patch_on_a_hidden_unit_loses_its_payload(self):
        unit = self.one("SELECT unit_id FROM unit_versions WHERE unit_version_id = ?", self.uvs(self.a.source_id)[0])[0]
        self.conn.execute("INSERT INTO patches(patch_id, unit_id, anchor_json, op, payload_json, replay_state, created_at) VALUES ('patch_x', ?, ?, 'set_text', ?, 'applied', 't')",
                          (unit, f'{{"quote": "{self.SECRET}"}}', f'{{"new": "corrected {self.SECRET}"}}'))
        self.decide("delete", {"target_id": self.a.source_id, "level": "b"})
        self.build()
        self.assertEqual(tuple(self.one("SELECT anchor_json, payload_json FROM patches WHERE patch_id = 'patch_x'")), ("{}", "{}"))

    def test_the_scrub_is_repeatable_and_the_second_run_changes_nothing(self):
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertEqual(commit.scrub_hidden_text(self.conn), {"statements": 0, "chunks": 0, "patches": 0, "proposals": 0, "locators": 0})

    def test_without_a_hide_nothing_is_touched(self):
        before = self.table_hash("statements", "chunks", "patches")
        self.assertEqual(commit.scrub_hidden_text(self.conn), {"statements": 0, "chunks": 0, "patches": 0, "proposals": 0, "locators": 0})
        self.assertEqual(self.table_hash("statements", "chunks", "patches"), before)

    def test_with_nothing_hidden_the_scrub_takes_no_write_transaction(self):
        """While another connection holds the write lock, a step that has nothing to do must not wait for it (it would time out, or take the lock)."""
        from kb import db
        other = db.connect(self.home.db_path)
        other.execute("PRAGMA busy_timeout = 100")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.assertEqual(commit.scrub_hidden_text(other), {"statements": 0, "chunks": 0, "patches": 0, "proposals": 0, "locators": 0})
        finally:
            self.conn.execute("ROLLBACK")
            other.close()

    def test_the_trigger_still_refuses_to_rewrite_a_statement_and_the_window_is_closed_afterwards(self):
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertEqual(self.one("SELECT count(*) FROM scrub_window")[0], 0)
        with self.assertRaises(Exception) as cm:
            self.conn.execute("UPDATE statements SET value_json = '\"x\"' WHERE subject_id = ?", (self.b.source_id,))
        self.assertIn("supersedes", str(cm.exception))

    def test_a_scrub_that_fails_half_way_changes_nothing_and_leaves_the_window_closed(self):
        from unittest import mock
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        before = self.rows_with(self.SECRET)
        with mock.patch.object(commit.audit, "append", side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                commit.scrub_hidden_text(self.conn)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.one("SELECT count(*) FROM scrub_window")[0], 0)
        self.assertEqual(self.rows_with(self.SECRET), before)
        self.assertEqual(self.build().outcome, "created")                         # and the next commit still does it
        self.assertEqual(self.rows_with(self.SECRET), (0, 0))

    def test_a_scrub_is_written_to_the_audit_log_with_counts_only(self):
        from kb import audit
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        event = [e for e in audit.events(self.conn) if e[2] == "text_scrubbed"][0]
        self.assertEqual(sorted(event[3]), ["chunks", "locators", "patches", "proposals", "statements"])
        self.assertGreater(event[3]["statements"], 0)

    def test_every_backup_goes_once_something_is_hidden_also_one_made_after_the_hide(self):
        """Second review: a backup made between the hide and the erasure holds the text too, and the first version kept every newer backup."""
        backups = self.home.sub("backups")
        old, new = os.path.join(backups, "before.bak"), os.path.join(backups, "after.bak")
        for path in (old, new):
            with open(path, "wb") as f:
                f.write(self.SECRET.encode())
        now = time.time()
        os.utime(old, (now - 3600, now - 3600))
        os.utime(new, (now + 3600, now + 3600))
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(new))
        self.assertEqual(inventory.check_after_hide(self.home, self.conn, self.SECRET), [])

    def test_with_nothing_hidden_the_scrub_does_not_even_open_a_write_transaction(self):
        from kb import db
        conn = db.connect(self.home.db_path, readonly=True)                           # a write transaction on this connection would fail
        try:
            self.assertEqual(commit.scrub_hidden_text(conn), {"statements": 0, "chunks": 0, "patches": 0, "proposals": 0, "locators": 0})
        finally:
            conn.close()

    def test_the_check_reports_a_backup_that_holds_the_text_while_it_is_still_there(self):
        """The expectation for the backups class is 'absent': a backup that holds the text is reported (here before the commit that removes it)."""
        with open(os.path.join(self.home.sub("backups"), "kept.bak"), "wb") as f:
            f.write(self.SECRET.encode())
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.assertIn("backups", [b[0] for b in inventory.check_after_hide(self.home, self.conn, self.SECRET)])

    def test_without_a_hide_the_backups_are_left_alone(self):
        path = os.path.join(self.home.sub("backups"), "keep.bak")
        with open(path, "wb") as f:
            f.write(b"x")
        self.assertEqual(commit.purge_old_backups(self.home, self.conn), 0)
        self.assertTrue(os.path.exists(path))

    # ---- second review (2026-11-20): proposals, working folders, damaged history, the bytes of the file
    def proposal_rows(self):
        return {pid: (kind, state, diff) for pid, kind, state, diff in self.conn.execute("SELECT proposal_id, kind, state, diff_json FROM proposals")}

    def test_a_proposal_that_names_the_hidden_source_is_blanked_in_every_state_and_the_others_stay(self):
        applied = self.decide("metadata", {"statements": [{"subject_id": self.a.source_id, "key": "note", "value": f"applied {self.SECRET}"}]})
        pending = review.create(self.conn, "metadata", {"statements": [{"subject_id": self.a.source_id, "key": "note", "value": f"pending {self.SECRET}"}]}, "roy")
        other = self.decide("metadata", {"statements": [{"subject_id": self.b.source_id, "key": "note", "value": "about beta only"}]})
        delete = self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        count = len(self.proposal_rows())
        self.build()
        rows = self.proposal_rows()
        self.assertEqual(len(rows), count)                                              # none is removed, only blanked
        self.assertEqual(rows[applied][2], commit.PURGED_DIFF)
        self.assertEqual((rows[pending][1], rows[pending][2]), ("stale", commit.PURGED_DIFF))        # it can no longer be carried out
        self.assertIn("about beta only", rows[other][2])
        self.assertIn(self.a.source_id, rows[delete][2])                                # the permission of the tombstone itself is kept
        self.assertEqual(self.one("SELECT count(*) FROM tombstones")[0], 1)
        self.assertFalse(inventory.scan(self.home, self.conn, self.SECRET)["db_proposals"])

    def test_the_proposal_trigger_still_refuses_an_edit_outside_the_window(self):
        pid = self.decide("metadata", {"statements": [{"subject_id": self.b.source_id, "key": "note", "value": "about beta only"}]})
        with self.assertRaises(Exception) as cm:
            self.conn.execute("UPDATE proposals SET diff_json = '{}' WHERE proposal_id = ?", (pid,))
        self.assertIn("never edited", str(cm.exception))

    def make_ticket(self, name, shift):
        d = os.path.join(self.home.path, "ask", "tickets", name)
        os.makedirs(d)
        with open(os.path.join(d, "prompt.txt"), "w", encoding="utf-8") as f:
            f.write(self.SECRET)
        stamp = time.time() + shift
        os.utime(d, (stamp, stamp))
        return d

    def test_a_working_folder_made_before_the_hide_or_older_than_an_hour_is_removed_and_a_later_one_stays(self):
        before, later, ancient = self.make_ticket("before", -60), self.make_ticket("later", 3600), self.make_ticket("ancient", -7200)
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertFalse(os.path.exists(before))
        self.assertFalse(os.path.exists(ancient))
        self.assertTrue(os.path.exists(later))

    def test_the_pending_report_counts_exactly_the_working_folders_that_the_removal_wanted_and_could_not_remove(self):
        """Fifth review: the removal and the report must ask the same question of each folder. One folder the removal wants but cannot remove, one it
        removes, one it never wanted: only the first is reported, and the report is empty once it is gone."""
        from unittest import mock
        stuck, gone, later = self.make_ticket("stuck", -60), self.make_ticket("gone", -7200), self.make_ticket("later", 3600)
        real = commit.shutil.rmtree

        def rmtree(path, *a, **kw):
            if os.path.basename(path) != "stuck":
                real(path, *a, **kw)
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        with mock.patch.object(commit.shutil, "rmtree", rmtree):
            r = self.build()
        self.assertEqual([line for line in r.erasure_pending if "working folder" in line], ["1 working folder(s) with hidden text could not be removed"])
        self.assertEqual((os.path.exists(stuck), os.path.exists(gone), os.path.exists(later)), (True, False, True))
        self.assertEqual(self.build().erasure_pending, [])
        self.assertEqual((os.path.exists(stuck), os.path.exists(later)), (False, True))

    @unittest.skipUnless(os.name == "nt", "an open file blocks its deletion on Windows")
    def test_an_opened_copy_that_a_viewer_still_holds_stays_until_it_is_let_go_and_is_then_removed(self):
        """Second review (needs_validation): `kb open` copies are removed at the next commit; one that another program holds open cannot be removed on
        Windows. It is left, the inventory check says so, and the next commit removes it."""
        sha = self.one("SELECT blob_sha256 FROM revisions WHERE source_id = ?", self.a.source_id)[0]
        folder = os.path.join(self.home.path, "opened", sha[:16])
        os.makedirs(folder)
        path = os.path.join(folder, "alpha.md")
        with open(path, "wb") as f:
            f.write(self.SECRET.encode())
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        held = open(path, "rb")
        try:
            self.build()
            self.assertTrue(os.path.exists(path))
            self.assertIn("opened_copies", [b[0] for b in inventory.check_after_hide(self.home, self.conn, self.SECRET)])
        finally:
            held.close()
        self.build()
        self.assertFalse(os.path.exists(folder))
        self.assertNotIn("opened_copies", [b[0] for b in inventory.check_after_hide(self.home, self.conn, self.SECRET)])

    def test_without_a_hide_only_an_hour_old_working_folder_is_removed(self):
        young, ancient = self.make_ticket("young", -60), self.make_ticket("ancient", -7200)
        self.add("gamma.md", "# Gamma\n\nanother page\n")
        self.build()
        self.assertTrue(os.path.exists(young))
        self.assertFalse(os.path.exists(ancient))

    def test_a_retired_commit_with_an_unreadable_manifest_goes_once_something_is_hidden_and_not_before(self):
        old = commit.active_commit_id(self.conn)
        self.add("gamma.md", "# Gamma\n\nanother page\n")
        self.build()                                                                     # `old` is retired
        manifest = os.path.join(commit.segment_dir(self.home, old), commit.MANIFEST)
        os.chmod(manifest, 0o666)
        with open(manifest, "wb") as f:
            f.write(b"{ not json")
        self.add("delta.md", "# Delta\n\nyet another page\n")
        self.build()
        self.assertTrue(os.path.isdir(commit.segment_dir(self.home, old)))               # nothing hidden: damage is for doctor, not for deletion
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertFalse(os.path.isdir(commit.segment_dir(self.home, old)))

    def test_a_patch_proposal_on_a_hidden_unit_is_blanked_too(self):
        uv = self.uvs(self.a.source_id)[1]
        pid = self.decide("patch", {"unit_version_id": uv, "op": "replace", "find": "First paragraph", "replace": f"Corrected paragraph {self.SECRET}"})
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertEqual(self.proposal_rows()[pid][2], commit.PURGED_DIFF)

    def test_a_commit_says_so_when_a_step_of_the_erasure_did_not_finish_and_the_next_one_finishes_it(self):
        """Third review, round 4: a commit that returns normally must not look like a finished erasure when a removal failed (a file held open, no permission)."""
        from unittest import mock
        stuck = os.path.join(self.home.sub("backups"), "stuck.bak")
        with open(stuck, "wb") as f:
            f.write(self.SECRET.encode())
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        with mock.patch.object(commit, "purge_old_backups", return_value=0):             # the removal fails
            r = self.build()
        self.assertEqual(len(r.erasure_pending), 1)
        self.assertIn("backup", r.erasure_pending[0])
        self.assertTrue(os.path.exists(stuck))
        again = self.build()
        self.assertEqual(again.erasure_pending, [])
        self.assertFalse(os.path.exists(stuck))

    def test_nothing_is_pending_when_nothing_is_hidden_even_with_leftovers_around(self):
        with open(os.path.join(self.home.sub("backups"), "keep.bak"), "wb") as f:
            f.write(b"x")
        self.assertEqual(commit.erasure_pending(self.home, self.conn), [])
        self.assertEqual(self.build(force=True).erasure_pending, [])

    def test_an_old_working_folder_is_not_pending_when_nothing_is_hidden(self):
        """With nothing hidden there is no promise to keep; a leftover of `kb ask` older than an hour is housekeeping, not an unfinished erasure."""
        ticket = os.path.join(self.home.path, "ask", "tickets", "old")
        os.makedirs(ticket)
        long_ago = time.time() - 7200
        os.utime(ticket, (long_ago, long_ago))
        self.assertEqual(commit._tickets_to_remove(self.home, self.conn), [ticket])         # the folder does qualify for removal ...
        self.assertEqual(commit.erasure_pending(self.home, self.conn), [])                  # ... but it is not reported

    def test_a_working_folder_that_could_not_be_removed_is_pending_after_a_hide(self):
        from unittest import mock
        ticket = os.path.join(self.home.path, "ask", "tickets", "running")
        os.makedirs(ticket)                                                                  # made before the hide: it may hold text that is hidden now
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        with mock.patch.object(commit, "purge_ask_tickets", return_value=0):
            r = self.build()
        self.assertTrue(any("working folder" in line for line in r.erasure_pending))
        self.assertTrue(os.path.isdir(ticket))
        self.assertEqual(self.build().erasure_pending, [])
        self.assertFalse(os.path.exists(ticket))

    def test_a_fresh_working_folder_that_its_question_could_not_remove_goes_at_the_next_commit(self):
        """Eighth review: neither the age rule nor the hide rule removes a folder that is new and made after the last hide."""
        tickets = os.path.join(self.home.path, "ask", "tickets")
        ticket = os.path.join(tickets, "ask_1")
        os.makedirs(ticket)
        self.assertEqual(commit._tickets_to_remove(self.home, self.conn), [])                # fresh, no hide: not removed by the old rules
        with open(ticket + commit.STALE_SUFFIX, "w", encoding="utf-8"):
            pass
        self.assertEqual(commit._tickets_to_remove(self.home, self.conn), [ticket])          # marked by its question: removed
        self.assertEqual(commit.purge_ask_tickets(self.home, self.conn), 1)
        self.assertEqual(os.listdir(tickets), [])                                            # folder and marker are both gone

    def test_a_marker_without_its_folder_is_cleaned_up_and_not_counted(self):
        tickets = os.path.join(self.home.path, "ask", "tickets")
        os.makedirs(tickets)
        marker = os.path.join(tickets, "ask_2" + commit.STALE_SUFFIX)
        with open(marker, "w", encoding="utf-8"):
            pass
        self.assertEqual(commit._tickets_to_remove(self.home, self.conn), [])
        self.assertFalse(os.path.exists(marker))

    def test_a_folder_that_is_both_marked_and_old_is_listed_once(self):
        tickets = os.path.join(self.home.path, "ask", "tickets")
        ticket = os.path.join(tickets, "ask_3")
        os.makedirs(ticket)
        long_ago = time.time() - 7200
        os.utime(ticket, (long_ago, long_ago))
        with open(ticket + commit.STALE_SUFFIX, "w", encoding="utf-8"):
            pass
        self.assertEqual(commit._tickets_to_remove(self.home, self.conn), [ticket])

    def test_a_marked_folder_that_stays_keeps_its_marker(self):
        from unittest import mock
        tickets = os.path.join(self.home.path, "ask", "tickets")
        ticket = os.path.join(tickets, "ask_4")
        os.makedirs(ticket)
        marker = ticket + commit.STALE_SUFFIX
        with open(marker, "w", encoding="utf-8"):
            pass
        with mock.patch.object(commit.shutil, "rmtree"):
            self.assertEqual(commit.purge_ask_tickets(self.home, self.conn), 0)
        self.assertTrue(os.path.exists(marker))

    def test_an_unfinished_compaction_is_pending_until_it_has_run(self):
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertEqual(commit.erasure_pending(self.home, self.conn), [])
        self.decide("delete", {"target_id": self.b.source_id, "level": "a"})
        commit.scrub_hidden_text(self.conn)
        self.assertIn("compaction", " ".join(commit.erasure_pending(self.home, self.conn)))
        self.assertTrue(commit.compact_after_scrub(self.conn))
        self.assertEqual(commit.erasure_pending(self.home, self.conn), [])

    def test_a_retired_version_that_could_not_be_removed_is_not_counted_as_removed(self):
        from unittest import mock
        from kb import audit
        old = commit.active_commit_id(self.conn)
        self.add("gamma.md", "# Gamma\n\nanother page\n")
        self.build()                                                                     # `old` is retired
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        with mock.patch.object(commit.shutil, "rmtree"):                                 # nothing can be removed
            self.assertEqual(commit.prune_stale_history(self.home, self.conn), [])
        self.assertTrue(os.path.isdir(commit.segment_dir(self.home, old)))
        self.assertFalse(any(e[2] == "history_pruned" for e in audit.events(self.conn)))
        self.assertIn(old, [c for c, _ in commit._stale_segments(self.home, self.conn)])

    def test_a_failed_compaction_is_recorded_and_tried_again_at_the_next_commit(self):
        import sqlite3
        from kb import audit
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertTrue(any(e[2] == "db_compacted" for e in audit.events(self.conn)))
        self.assertFalse(commit.compact_after_scrub(self.conn))                           # nothing new was scrubbed: nothing to do
        self.decide("delete", {"target_id": self.b.source_id, "level": "a"})
        commit.scrub_hidden_text(self.conn)                                               # a second scrub that changes rows

        class Failing:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *args):
                if sql.startswith("VACUUM"):
                    raise sqlite3.OperationalError("database or disk is full")
                return self.conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.conn, name)
        self.assertFalse(commit.compact_after_scrub(Failing(self.conn)))
        self.assertEqual([e[2] for e in audit.events(self.conn)].count("compaction_failed"), 1)
        self.assertTrue(commit.compact_after_scrub(self.conn))                            # the retry at the next commit
        self.assertFalse(commit.compact_after_scrub(self.conn))


class TestTheBytesOfTheFile(World):
    """Second review, measured: SQLite does not overwrite what an UPDATE replaces, so after a hide and a commit the old text was still in the database
    file and its log. A paragraph that needs overflow pages is used because those are the pages that stay behind."""
    SECRET = "zqbytesx58"

    def setUp(self):
        super().setUp()
        self.a = self.add("alpha.md", f"# Alpha\n\nFirst paragraph with {self.SECRET} inside. " + "filler words go here. " * 1500 + "\n\nSecond one.\n")
        self.add("beta.md", "# Beta\n\nBeta stays.\n")
        self.build()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")                              # the text is now in the main file, as in a database that has been used for a while

    def test_the_text_is_in_the_file_before_hiding_a_negative_control(self):
        self.assertTrue(inventory.scan(self.home, self.conn, self.SECRET)["db_file_bytes"])

    def test_after_a_hide_and_a_commit_it_is_in_neither_the_file_nor_the_log(self):
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertEqual(inventory.check_after_hide(self.home, self.conn, self.SECRET), [])
        for suffix in ("", "-wal"):
            path = self.home.db_path + suffix
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    self.assertNotIn(self.SECRET.encode(), f.read(), suffix or "main file")

    def test_sqlite_vacuum_keeps_the_relative_order_of_rows_that_programs_read_by_rowid(self):
        """Third review, round 4: several queries here order by the bare rowid of tables that have no integer primary key (statements, conversions, unit
        versions). SQLite does not promise to keep rowids at VACUUM; this checks that the order survives it, with holes in the numbering."""
        import shutil
        import sqlite3
        import tempfile
        d = tempfile.mkdtemp(prefix="kb_vac_")
        self.addCleanup(shutil.rmtree, d, True)
        c = sqlite3.connect(os.path.join(d, "t.db"), isolation_level=None)
        self.addCleanup(c.close)
        c.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
        for i in range(60):
            c.execute("INSERT INTO t VALUES (?, ?)", (f"k{(i * 37) % 60:02d}", "x" * 200))
        c.execute("DELETE FROM t WHERE rowid % 3 = 0")
        before = [r[0] for r in c.execute("SELECT k FROM t ORDER BY rowid")]
        c.execute("VACUUM")
        self.assertEqual([r[0] for r in c.execute("SELECT k FROM t ORDER BY rowid")], before)

    def test_the_order_of_the_real_tables_is_the_same_after_the_compaction(self):
        from kb import audit
        tables = {"statements": "statement_id", "conversions": "conv_id", "unit_versions": "unit_version_id"}
        before = {t: [r[0] for r in self.conn.execute(f"SELECT {k} FROM {t} ORDER BY rowid")] for t, k in tables.items()}
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertTrue(any(e[2] == "db_compacted" for e in audit.events(self.conn)))
        for t, k in tables.items():
            now = [r[0] for r in self.conn.execute(f"SELECT {k} FROM {t} ORDER BY rowid")]
            self.assertEqual([x for x in now if x in set(before[t])], before[t], t)           # rows that existed keep their relative order

    def test_the_other_source_and_the_integrity_of_the_database_survive_the_compaction(self):
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        self.build()
        self.assertEqual(self.one("PRAGMA integrity_check")[0], "ok")
        self.assertGreater(self.one("SELECT count(*) FROM statements WHERE instr(value_json, 'Beta stays') > 0")[0], 0)
        self.assertEqual(Searcher(self.home, self.conn).sources("Beta", 3)[0].title, "Beta")


class TestNoHashOfHiddenTextLeftForAGuesser(World):
    """Eighth review, decision of Roy 2026-11-24: after a hide, someone who holds the database and guesses the paragraph must not be able to confirm the
    guess from a hash. Measured before the keyed hashes: the fingerprint column, the id of the unit version and the id of its statement all matched."""
    SECRET = "The heron hides in the reeds of the harbour at night."

    def setUp(self):
        super().setUp()
        self.doc = self.add("heron.md", f"# Heron\n\n{self.SECRET}\n\nAnother paragraph that stays.\n")
        self.other = self.add("other.md", "# Other\n\nNothing secret here.\n")
        self.build()
        self.uv = self.uvs(self.doc.source_id)[1]
        self.unit_id, self.locator, self.conv_id, self.kind = self.one(
            "SELECT unit_id, locator_json, conv_id, kind FROM unit_versions WHERE unit_version_id = ?", self.uv)
        self.heading = json.loads(self.one("SELECT locator_json FROM conversion_units WHERE conv_id = ? AND unit_version_id = ?", self.conv_id, self.uv)[0])["heading"]
        self.right_key = ids._KEY
        self.units = [[kind, text, json.loads(loc)["heading"]] for kind, text, loc in self.conn.execute(
            "SELECT uv.kind, json_extract(s.value_json, '$'), cu.locator_json FROM conversion_units cu JOIN unit_versions uv USING (unit_version_id) "
            "JOIN statements s ON s.subject_id = cu.unit_version_id AND s.key = 'text' WHERE cu.conv_id = ? ORDER BY cu.ordinal", (self.conv_id,))]
        self.decide("delete", {"target_id": self.unit_id, "level": "a"})
        self.build()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def everything_in_the_folder(self):
        """Every value of every table, and the bytes of every file of the data folder except the original files."""
        blobs = []
        for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for row in self.conn.execute(f"SELECT * FROM {name}"):
                blobs.append(" ".join(str(v) for v in row).encode("utf-8"))
        for root, _dirs, names in os.walk(self.home.path):
            if os.sep + "blobs" in root:
                continue
            for n in names:
                with open(os.path.join(root, n), "rb") as f:
                    blobs.append(f.read())
        return blobs

    def test_the_plain_hashes_of_the_text_are_nowhere(self):
        text = self.SECRET
        plain = {
            "text": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "unit": hashlib.sha256(canonical_bytes({"kind": self.kind, "text": text, "heading_path": self.heading})).hexdigest(),
            "whole conversion": hashlib.sha256(canonical_bytes(self.units)).hexdigest(),
        }
        self.assertIn(text, [u[1] for u in self.units])                                      # the preimage of the last one really holds the paragraph
        data = self.everything_in_the_folder()
        for label, digest in plain.items():
            self.assertFalse(any(digest.encode() in blob for blob in data), label)
        self.assertFalse(any(text.encode() in blob for blob in data))

    def derived(self, unit_id, locator, conv_id):
        """What a person who guesses the paragraph can work out with the code and a key: the fingerprint, the unit version id, the statement id."""
        guess = cb.Unit(self.kind, self.SECRET, {}, tuple(self.heading))
        fp = ids.fingerprint(self.SECRET)
        uv = ids.unit_version_id(unit_id, pipeline.unit_output_sha256(guess), json.loads(locator), fp)
        st = ids.statement_id(self.uv, "text", self.SECRET, "native", {"conv_id": conv_id})
        return fp, uv, st

    def present(self, fp, uv, st):
        return (self.one("SELECT count(*) FROM unit_versions WHERE fingerprint = ?", fp)[0],
                self.one("SELECT count(*) FROM unit_versions WHERE unit_version_id = ?", uv)[0],
                self.one("SELECT count(*) FROM statements WHERE statement_id = ?", st)[0])

    def test_the_owner_of_the_key_can_still_follow_the_text_so_the_test_means_something(self):
        self.assertEqual(self.present(*self.derived(self.unit_id, self.locator, self.conv_id)), (1, 1, 1))

    def test_a_guesser_without_the_key_confirms_nothing(self):
        ids.use_key(bytes(32))                                                             # not the key of this folder
        try:
            self.assertEqual(self.present(*self.derived(self.unit_id, self.locator, self.conv_id)), (0, 0, 0))
        finally:
            ids.use_key(self.right_key)


class TestWhatStandsAroundHiddenText(World):
    """Ninth review: the headings above a hidden unit and its offsets in the file were stored as plain data in the locators and survived a hide. Measured before:
    hiding a source left every heading of it readable in `unit_versions` and `conversion_units`."""

    def setUp(self):
        super().setUp()
        self.doc = self.add("zebra.md", "# Zebracolony\n\nGreyparagraph about nothing.\n\n## Quaggasection\n\nSecondparagraph here.\n")
        self.other = self.add("other.md", "# Other\n\nFine.\n")
        self.build()

    def every_value(self):
        blobs = []
        for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for row in self.conn.execute(f"SELECT * FROM {name}"):
                blobs.append(" ".join(str(v) for v in row).encode("utf-8"))
        for root, _dirs, names in os.walk(self.home.path):
            if os.sep + "blobs" in root:
                continue
            for n in names:
                with open(os.path.join(root, n), "rb") as f:
                    blobs.append(f.read())
        return blobs

    def test_hiding_a_source_leaves_none_of_its_words_anywhere_but_in_the_original_file(self):
        words = [w.encode() for w in ("Zebracolony", "Greyparagraph", "Quaggasection", "Secondparagraph")]
        self.assertTrue(all(any(w in b for b in self.every_value()) for w in words))               # the check can see them before
        self.decide("delete", {"target_id": self.doc.source_id, "level": "a"})
        self.build()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        data = self.every_value()
        for w in words:
            self.assertFalse(any(w in b for b in data), w)

    def test_hiding_a_unit_erases_where_it_stood_and_leaves_its_neighbours_alone(self):
        uvs = self.uvs(self.doc.source_id)
        hidden_uv = uvs[1]
        unit_id = self.one("SELECT unit_id FROM unit_versions WHERE unit_version_id = ?", hidden_uv)[0]
        before = {uv: self.one("SELECT locator_json FROM conversion_units WHERE unit_version_id = ?", uv)[0] for uv in uvs}
        self.assertIn("Zebracolony", before[hidden_uv])
        self.decide("delete", {"target_id": unit_id, "level": "a"})
        self.build()
        for uv in uvs:
            now = self.one("SELECT locator_json FROM conversion_units WHERE unit_version_id = ?", uv)[0]
            self.assertEqual(now, "{}" if uv == hidden_uv else before[uv])

    def test_a_locator_is_not_rewritten_outside_the_scrub_window_and_its_identity_never(self):
        row = self.one("SELECT conv_id, ordinal FROM conversion_units LIMIT 1")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE conversion_units SET locator_json = '{}' WHERE conv_id = ? AND ordinal = ?", row)
        self.conn.execute("INSERT INTO scrub_window(id) VALUES (1)")
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute("UPDATE conversion_units SET ordinal = ordinal + 100 WHERE conv_id = ? AND ordinal = ?", row)
            self.conn.execute("UPDATE conversion_units SET locator_json = '{}' WHERE conv_id = ? AND ordinal = ?", row)              # inside the window: allowed
        finally:
            self.conn.execute("DELETE FROM scrub_window")

    def test_the_structural_key_holds_no_heading(self):
        keys = [json.loads(loc)["key"] for (loc,) in self.conn.execute("SELECT locator_json FROM unit_versions")]
        self.assertTrue(keys)
        self.assertFalse(any("Zebracolony" in k or "Quaggasection" in k for k in keys))

    def test_after_a_unit_is_hidden_the_rest_of_the_source_still_works_with_its_empty_locator(self):
        """Tenth review: the readers of the locator (search, patches, open) were read, not run, against a hidden unit whose locator is now `{}`."""
        uvs = self.uvs(self.doc.source_id)
        unit_id = self.one("SELECT unit_id FROM unit_versions WHERE unit_version_id = ?", uvs[1])[0]
        self.decide("delete", {"target_id": unit_id, "level": "a"})
        self.build()
        self.assertIn(self.doc.source_id, self.hit_sources("Secondparagraph"))
        self.assertNotIn(self.doc.source_id, self.hit_sources("Greyparagraph"))
        self.decide("patch", {"unit_version_id": uvs[3], "op": "replace", "find": "here", "replace": "there"})
        self.build()
        self.assertIn(self.doc.source_id, self.hit_sources("there"))
        s = Searcher(self.home, self.conn)
        self.assertTrue(s.sources("Secondparagraph", 3))
