"""S1d: the commit protocol (T-C1 crash and artifact-level injection, T-C2 ghost check on content, T-C3 history, recovery, rollback, gc).
Every test states the failure it guards: a commit that is half published, an index that disagrees with its members, or a version that changes
after it was published are the failures that make a knowledge base quietly wrong."""
import errno
import json
import os
import shutil
import sqlite3
import threading
import time
import unittest
from unittest import mock

from kb import commit, ftsindex, ids
from kb.errors import ArtifactsMissing, CommitFailed
from kb.profile import Profile
from kb import colls

from .support import approved_proposal, run_py
from .world import World

NT = os.name == "nt"


class TestBuild(World):
    def test_first_build_publishes_a_complete_commit(self):
        self.seed_docs()
        r = self.build()
        self.assertEqual(r.outcome, "created")
        self.assertEqual(self.states(), {r.commit_id: "active"})
        self.assertEqual(commit.active_commit_id(self.conn), r.commit_id)
        self.assertEqual(os.listdir(self.home.sub("staging")), [])
        manifest, problems = commit.verify_artifacts(commit.segment_dir(self.home, r.commit_id))
        self.assertEqual(problems, [])
        self.assertEqual(manifest["counts"]["sources"], 3)
        self.assertEqual(manifest["counts"]["chunks"], r.chunks)
        self.assertEqual(self.one("SELECT count(*) FROM commit_chunks WHERE commit_id=?", r.commit_id)[0], r.chunks)
        self.assertEqual(self.one("SELECT count(*) FROM statements WHERE state='candidate'")[0], 0)      # published statements are committed
        self.assertIn(self.a.source_id, self.hit_sources("falcon harbor"))
        self.assertIn(self.b.source_id, self.hit_sources("貝氏推論"))

    def test_an_unchanged_knowledge_base_is_not_committed_again(self):
        """Idempotence: running the build twice must not stack identical versions."""
        self.seed_docs()
        first = self.build()
        again = self.build()
        self.assertEqual((again.outcome, again.commit_id), ("unchanged", first.commit_id))
        self.assertEqual(self.one("SELECT count(*) FROM commits")[0], 1)

    def test_a_text_that_repeats_itself_gives_one_chunk_per_distinct_content_and_the_commit_succeeds(self):
        """Found on real data (a PDF whose page repeats one line): equal windows of one unit are equal chunks, so the chunk id repeats.
        The commit used to fail on its own primary key; a chunk is its content, so the same content is kept once."""
        line = "abcdefghijklmnopqrstuvwxyz0123456789ABC "                     # 40 characters, no break mark: the windows are cut at exactly 2400
        self.add("loop.md", "# Loop\n\n" + line * 200 + "\n")                  # 8000 characters: three equal windows of 2400 and a rest
        r = self.build()
        self.assertEqual(r.outcome, "created")
        ids_in_commit = [c for (c,) in self.conn.execute("SELECT chunk_id FROM commit_chunks WHERE commit_id=?", (r.commit_id,))]
        self.assertEqual(len(ids_in_commit), len(set(ids_in_commit)))
        self.assertEqual(r.chunks, len(ids_in_commit))
        asm = commit.assemble(self.conn)
        texts = [c.text for c in asm.chunks]
        self.assertEqual(len(texts), len(set(texts)))                            # nothing is kept twice ...
        self.assertEqual(sum(1 for t in texts if len(t) == 2400), 1)             # ... the repeated window once (three equal windows were cut) ...
        self.assertEqual(asm.repeated_chunks, 2)                                 # ... and the two left out are counted, not silent
        self.assertEqual(r.chunks, len(texts))
        self.assertEqual(commit.verify_artifacts(commit.segment_dir(self.home, r.commit_id))[1], [])

    def test_the_same_text_in_two_sources_keeps_each_source_findable(self):
        """A chunk is cut from one source's units, so equal text in two sources must not make one source vanish from the commit."""
        self.add("one.md", "# Same\n\nThe heron crossed the quiet harbor at dawn.\n")
        self.add("two.md", "# Same\n\nThe heron crossed the quiet harbor at dawn.\n")
        r = self.build()
        self.assertEqual(r.outcome, "created")
        self.assertEqual(len(self.hit_sources("heron")), 2)

    def test_a_change_makes_a_new_commit_and_retires_the_old_one(self):
        self.seed_docs()
        first = self.build()
        self.add("alpha.md", "# Alpha\n\nThe heron replaced the falcon.\n")
        second = self.build()
        self.assertEqual(second.outcome, "created")
        self.assertEqual(self.states(), {first.commit_id: "retired", second.commit_id: "active"})
        self.assertEqual(self.one("SELECT parent_commit_id FROM commits WHERE commit_id=?", second.commit_id)[0], first.commit_id)
        self.assertIn(self.a.source_id, self.hit_sources("heron"))
        self.assertNotIn(self.a.source_id, self.hit_sources("harbor"))              # the old wording is gone from the active index
        self.assertTrue(os.path.isdir(commit.segment_dir(self.home, first.commit_id)))   # but the old commit is kept for rollback

    def test_a_changed_profile_is_a_change(self):
        """Visibility is part of what a commit commits to: new rules for the same text need a new commit."""
        self.seed_docs()
        first = self.build()
        self.conn.execute("UPDATE collections SET profile_json=? WHERE collection_id=?", (Profile(agent_visible=True).to_json(), self.cid))
        self.assertEqual(self.build().outcome, "created")
        self.assertNotEqual(commit.active_commit_id(self.conn), first.commit_id)

    def test_a_published_commit_reads_the_same_forever(self):
        """T-C3: what commit X contained is fixed; later conversions, edits and commits never change it."""
        self.seed_docs()
        x = self.build()
        before = {k: sorted(r[0] for r in self.conn.execute(f"SELECT {k[1]} FROM {k[0]} WHERE commit_id=?", (x.commit_id,)))
                  for k in (("commit_units", "unit_version_id"), ("commit_statements", "statement_id"), ("commit_chunks", "chunk_id"))}
        texts = {cid: self.chunk_text(cid) for cid in before[("commit_chunks", "chunk_id")]}
        manifest_before = commit.read_manifest(commit.segment_dir(self.home, x.commit_id))
        self.add("alpha.md", "# Alpha\n\ncompletely different text now\n")
        self.add("delta.md", "# Delta\n\nnew document\n")
        self.build()
        after = {k: sorted(r[0] for r in self.conn.execute(f"SELECT {k[1]} FROM {k[0]} WHERE commit_id=?", (x.commit_id,)))
                 for k in (("commit_units", "unit_version_id"), ("commit_statements", "statement_id"), ("commit_chunks", "chunk_id"))}
        self.assertEqual(after, before)
        self.assertEqual({cid: self.chunk_text(cid) for cid in before[("commit_chunks", "chunk_id")]}, texts)
        self.assertEqual(commit.read_manifest(commit.segment_dir(self.home, x.commit_id)), manifest_before)

    def test_tombstoned_sources_and_units_are_left_out(self):
        """A level a/b tombstone (S2 creates them) must already be honoured by assembly: hidden content is not committed."""
        self.seed_docs()
        self.build()
        approved_proposal(self.conn, "pr_x", diff=json.dumps({"target_id": self.a.source_id, "level": "a"}))
        self.conn.execute("INSERT INTO tombstones(tombstone_id,target_id,level,proposal_id,at) VALUES ('tomb_1',?, 'a','pr_x','t')", (self.a.source_id,))
        r = self.build()
        self.assertEqual(r.outcome, "created")
        self.assertNotIn(self.a.source_id, self.hit_sources("falcon"))
        self.assertIn(self.b.source_id, self.hit_sources("貝氏"))
        self.assertEqual(commit.read_manifest(commit.segment_dir(self.home, r.commit_id))[0]["counts"]["sources"], 2)

    def test_a_collection_that_forbids_plaintext_indexing_is_committed_but_not_indexed(self):
        """P7 enforcement point 2: members exist, no chunk reaches the index, and the manifest says how many sources were withheld."""
        secret = colls.create(self.conn, "diary", Profile(sensitivity="secret", index_plaintext=False))
        s = self.add("private.txt", "my secret plan about the harbor", collection=secret)
        self.seed_docs()
        r = self.build()
        self.assertNotIn(s.source_id, self.hit_sources("secret plan"))
        self.assertEqual(self.search("secret plan"), [])
        manifest = commit.read_manifest(commit.segment_dir(self.home, r.commit_id))[0]
        self.assertEqual(manifest["counts"]["not_indexed_sources"], 1)
        self.assertEqual(self.one("SELECT count(*) FROM commit_units cu JOIN unit_versions uv ON uv.unit_version_id=cu.unit_version_id "
                                  "JOIN conversion_units x ON x.unit_version_id=uv.unit_version_id JOIN conversions c ON c.conv_id=x.conv_id "
                                  "JOIN revisions rv ON rv.revision_id=c.revision_id WHERE cu.commit_id=? AND rv.source_id=?",
                                  r.commit_id, s.source_id)[0], 1)

    def test_display_roles_are_frozen_into_the_commit(self):
        """A second statement for the same subject and key is an alternate; the native one is displayed."""
        self.seed_docs()
        from kb import pipeline
        pipeline.process_pending(self.conn, self.store)
        derived = ids.statement_id(self.a.source_id, "title", "A guessed title", "derived", {"x": 1})
        self.conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,state) VALUES (?,?,?,?,?,?, 'candidate')",
                          (derived, self.a.source_id, "title", '"A guessed title"', "derived", '{"x": 1}'))
        r = self.build()
        roles = dict(self.conn.execute("SELECT statement_id, role FROM commit_statements WHERE commit_id=?", (r.commit_id,)))
        self.assertEqual(roles[derived], "alternate")
        shown = [sid for sid, role in roles.items() if role == "display" and self.one("SELECT key FROM statements WHERE statement_id=?", sid)[0] == "title"
                 and self.one("SELECT subject_id FROM statements WHERE statement_id=?", sid)[0] == self.a.source_id]
        self.assertEqual(len(shown), 1)
        self.assertEqual(self.one("SELECT origin FROM statements WHERE statement_id=?", shown[0])[0], "native")


class TestGhostCheck(World):
    """T-C2: the index must match the members in CONTENT, not just in id lists. Each tamper keeps the file hash consistent with the manifest
    (it happens before the hash is taken), so only the content check can notice."""

    def tamper_build(self, tamper):
        real = ftsindex.build

        def wrapper(path, rows):
            n = real(path, rows)
            conn = sqlite3.connect(path)
            try:
                tamper(conn)
                conn.commit()
            finally:
                conn.close()
            return n
        ftsindex.build = wrapper
        self.addCleanup(setattr, ftsindex, "build", real)

    def attempt(self, tamper, expect):
        self.seed_docs()
        old = self.build()
        self.add("alpha.md", "# Alpha\n\nchanged so that a new commit is needed\n")
        self.tamper_build(tamper)
        with self.assertRaises(CommitFailed) as cm:
            self.build()
        self.assertIn(expect, str(cm.exception))
        self.assertEqual(commit.active_commit_id(self.conn), old.commit_id)             # the previous version is untouched and still served
        commit.active_artifacts(self.home, self.conn)
        self.assertEqual(os.listdir(self.home.sub("staging")), [])
        self.assertEqual(sorted(s for s in self.states().values()), ["abandoned", "active"])

    def test_a_ghost_chunk_in_the_index(self):
        def add_ghost(c):
            n = c.execute("SELECT max(ordinal) FROM chunk_map").fetchone()[0] + 1
            c.execute("INSERT INTO fts(rowid, tokens) VALUES (?, 'ghost')", (n,))
            c.execute("INSERT INTO chunk_map VALUES (?, 'ck_ghostghostghostghostghost', 'src_x', ?)", (n, "0" * 64))
        self.attempt(add_ghost, "ghost chunks in index")

    def test_a_chunk_missing_from_the_index(self):
        def drop(c):
            c.execute("DELETE FROM fts WHERE rowid = 1")
            c.execute("DELETE FROM chunk_map WHERE ordinal = 1")
        self.attempt(drop, "missing from index")

    def test_right_id_wrong_content(self):
        """The case a list-of-ids comparison cannot see: every id is present, one chunk's indexed words are not its text's words."""
        self.attempt(lambda c: c.execute("UPDATE fts SET tokens = 'something else entirely' WHERE rowid = 1"), "index tokens differ")

    def test_right_id_wrong_text_hash(self):
        self.attempt(lambda c: c.execute("UPDATE chunk_map SET text_sha256 = ? WHERE ordinal = 1", ("f" * 64,)), "text hash differs")

    def test_right_id_wrong_source(self):
        self.attempt(lambda c: c.execute("UPDATE chunk_map SET source_id = 'src_someoneelse' WHERE ordinal = 1"), "source differs")

    def test_visibility_inputs_changed_during_the_build(self):
        """If a profile or tombstone changes between assembly and verification the commit is refused rather than published with stale rules."""
        self.seed_docs()
        old = self.build()
        self.add("alpha.md", "# Alpha\n\nanother change\n")
        real = commit._stage_members

        def stage_then_change(conn, commit_id, asm):
            real(conn, commit_id, asm)
            conn.execute("UPDATE collections SET profile_json = ? WHERE collection_id = ?", (Profile(agent_visible=True).to_json(), self.cid))
        commit._stage_members = stage_then_change
        self.addCleanup(setattr, commit, "_stage_members", real)
        with self.assertRaises(CommitFailed) as cm:
            self.build()
        self.assertIn("visibility inputs changed", str(cm.exception))
        self.assertEqual(commit.active_commit_id(self.conn), old.commit_id)


class TestInjectedFailures(World):
    """T-C1 artifact level: the environment fails in the ways real machines do. The previous commit must always survive and keep serving."""

    def prepare(self):
        self.seed_docs()
        self.old = self.build()
        self.add("alpha.md", "# Alpha\n\nnew wording about owls\n")

    def assert_old_survives(self):
        self.assertEqual(commit.active_commit_id(self.conn), self.old.commit_id)
        self.assertIn(self.a.source_id, self.hit_sources("falcon"))
        self.assertEqual(os.listdir(self.home.sub("staging")), [])
        self.assertEqual(sorted(self.states().values()), ["abandoned", "active"])

    def test_disk_full_while_writing_the_manifest(self):
        self.prepare()
        real = commit._write_file

        def full(path, data):
            raise OSError(errno.ENOSPC, "No space left on device")
        commit._write_file = full
        self.addCleanup(setattr, commit, "_write_file", real)
        with self.assertRaises(CommitFailed):
            self.build()
        self.assert_old_survives()

    def test_disk_full_while_building_the_index(self):
        self.prepare()
        real = ftsindex.build

        def full(path, rows):
            raise sqlite3.OperationalError("database or disk is full")
        ftsindex.build = full
        self.addCleanup(setattr, ftsindex, "build", real)
        with self.assertRaises(CommitFailed):
            self.build()
        self.assert_old_survives()

    def test_a_truncated_artifact_is_caught_before_publishing(self):
        self.prepare()
        real = commit._write_artifacts

        def truncating(directory, commit_id, parent, asm, *extra):
            sha = real(directory, commit_id, parent, asm, *extra)
            with open(os.path.join(directory, commit.FTS), "r+b") as f:
                f.truncate(100)
            return sha
        commit._write_artifacts = truncating
        self.addCleanup(setattr, commit, "_write_artifacts", real)
        with self.assertRaises(CommitFailed) as cm:
            self.build()
        self.assertIn("does not match the manifest", str(cm.exception))
        self.assert_old_survives()

    def test_rename_that_always_fails_abandons_after_retrying(self):
        self.prepare()
        sleeps = []
        real_sleep, real_rename = time.sleep, commit._rename_dir
        commit.time.sleep = lambda s: sleeps.append(s)
        commit._rename_dir = lambda a, b: (_ for _ in ()).throw(PermissionError(13, "held by another program"))
        self.addCleanup(setattr, commit.time, "sleep", real_sleep)
        self.addCleanup(setattr, commit, "_rename_dir", real_rename)
        with self.assertRaises(CommitFailed) as cm:
            self.build()
        self.assertIn("could not rename", str(cm.exception))
        self.assertEqual(len(sleeps), commit.RENAME_TRIES)
        self.assert_old_survives()

    def test_a_temporarily_held_folder_is_retried_until_it_is_free(self):
        self.prepare()
        real_sleep, real_rename = time.sleep, commit._rename_dir
        fails = [2]

        def flaky(a, b):
            if fails[0]:
                fails[0] -= 1
                raise PermissionError(13, "held")
            return real_rename(a, b)
        commit._rename_dir = flaky
        commit.time.sleep = lambda s: None
        self.addCleanup(setattr, commit, "_rename_dir", real_rename)
        self.addCleanup(setattr, commit.time, "sleep", real_sleep)
        self.assertEqual(self.build().outcome, "created")
        self.assertIn(self.a.source_id, self.hit_sources("owls"))

    @unittest.skipUnless(NT, "holds a real Windows handle without delete sharing")
    def test_a_real_open_handle_blocks_the_rename_until_it_is_released(self):
        """What an antivirus scanner does to a fresh folder: the rename fails while the handle is open, succeeds after, and the commit completes."""
        import ctypes
        self.prepare()
        real = commit._write_artifacts
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p

        def holding(directory, commit_id, parent, asm, *extra):
            sha = real(directory, commit_id, parent, asm, *extra)
            handle = k32.CreateFileW(os.path.join(directory, commit.FTS), 0x80000000, 1, None, 3, 0, None)    # read, share read only
            threading.Timer(0.4, lambda: k32.CloseHandle(ctypes.c_void_p(handle))).start()
            return sha
        commit._write_artifacts = holding
        self.addCleanup(setattr, commit, "_write_artifacts", real)
        self.assertEqual(self.build().outcome, "created")

    def test_corruption_right_after_the_rename_is_caught_by_the_read_back(self):
        self.prepare()
        real = commit._rename_dir

        def rename_then_corrupt(a, b):
            real(a, b)
            with open(os.path.join(b, commit.FTS), "r+b") as f:
                f.write(b"X" * 50)
        commit._rename_dir = rename_then_corrupt
        self.addCleanup(setattr, commit, "_rename_dir", real)
        with self.assertRaises(CommitFailed) as cm:
            self.build()
        self.assertIn("read-back", str(cm.exception))
        self.assertEqual(commit.active_commit_id(self.conn), self.old.commit_id)
        self.assertIn(self.a.source_id, self.hit_sources("falcon"))
        self.assertEqual(self.states()[[c for c, s in self.states().items() if s == "abandoned"][0]], "abandoned")


class TestCrashRecovery(World):
    """T-C1: kill the process at every step; after recovery the active commit is complete and readable, and it is either the old or the new one."""

    POINTS = {"commit.begin": False, "commit.after_artifacts": False, "commit.after_verify": False, "commit.after_journal": True,
              "commit.after_rename": True, "commit.after_readback": True, "commit.before_activate": True, "commit.after_activate": True}

    def kill_at(self, point):
        code = (f"from kb import commit,db,pipeline,cas;from kb.home import Home;h=Home({self.home.path!r});c=db.connect(h.db_path);"
                f"pipeline.process_pending(c,cas.BlobStore(h.sub('blobs')));commit.build(h,c)")
        r = run_py(code, {"KB_FAULT": point})
        self.assertEqual(r.returncode, 137, r.stderr[-400:])

    def test_every_kill_point_with_a_previous_commit(self):
        for point, expect_new in self.POINTS.items():
            with self.subTest(point=point):
                self.setUp()
                self.seed_docs()
                old = self.build()
                self.add("alpha.md", f"# Alpha\n\nchange for {point} about owls\n")
                self.kill_at(point)
                conn = self.conn
                actions = commit.recover(self.home, conn)
                active = commit.active_commit_id(conn)
                cid, manifest, fts = commit.active_artifacts(self.home, conn)              # readable immediately
                self.assertEqual(commit.verify_artifacts(commit.segment_dir(self.home, active))[1], [])
                self.assertEqual(active == old.commit_id, not expect_new, (point, actions))
                self.assertEqual(bool(self.hit_sources("owls")), expect_new)
                self.assertEqual(os.listdir(self.home.sub("staging")), [])
                self.assertEqual([s for s in self.states().values() if s in ("staging", "verified", "publishing")], [])
                self.assertEqual(sum(1 for s in self.states().values() if s == "active"), 1)
                self.assertEqual(self.build(force=False).outcome in ("created", "unchanged"), True)     # and a new build works

    def test_every_kill_point_on_the_very_first_commit(self):
        for point, expect_new in self.POINTS.items():
            with self.subTest(point=point):
                self.setUp()
                self.seed_docs()
                self.kill_at(point)
                commit.recover(self.home, self.conn)
                if expect_new:
                    commit.active_artifacts(self.home, self.conn)
                    self.assertIn(self.a.source_id, self.hit_sources("falcon"))
                else:
                    self.assertIsNone(commit.active_commit_id(self.conn))
                    with self.assertRaises(ArtifactsMissing):
                        commit.active_artifacts(self.home, self.conn)

    def test_recovery_is_idempotent(self):
        self.seed_docs()
        self.kill_at("commit.after_journal")
        first = commit.recover(self.home, self.conn)
        self.assertEqual(commit.recover(self.home, self.conn), [])
        self.assertTrue(first)


class TestLaterDamage(World):
    def test_a_lost_directory_entry_is_repaired_from_staging(self):
        """The rename was not durable although the database says active: the folder is still under its staging name."""
        self.seed_docs()
        r = self.build()
        shutil.move(commit.segment_dir(self.home, r.commit_id), commit.staging_dir(self.home, r.commit_id))
        with self.assertRaises(ArtifactsMissing):
            commit.active_artifacts(self.home, self.conn)
        self.assertEqual(commit.recover(self.home, self.conn), [(r.commit_id, "re-published")])
        commit.active_artifacts(self.home, self.conn)

    def test_files_that_are_simply_gone_are_refused_not_silently_replaced(self):
        self.seed_docs()
        old = self.build()
        self.add("alpha.md", "# Alpha\n\nnew\n")
        new = self.build()
        shutil.rmtree(commit.segment_dir(self.home, new.commit_id))
        with self.assertRaises(ArtifactsMissing) as cm:
            commit.active_artifacts(self.home, self.conn)
        self.assertIn("rollback", str(cm.exception))
        self.assertEqual(commit.recover(self.home, self.conn), [])                    # recovery does not guess
        self.assertEqual(commit.rollback(self.home, self.conn), old.commit_id)         # the explicit step
        self.assertIn(self.a.source_id, self.hit_sources("falcon"))
        self.assertEqual(sorted(self.states().values()), ["active", "retired"])

    def test_rollback_needs_a_verified_previous_commit(self):
        self.seed_docs()
        only = self.build()
        with self.assertRaises(CommitFailed):
            commit.rollback(self.home, self.conn)                                       # nothing before it
        self.add("alpha.md", "# Alpha\n\nsecond\n")
        self.build()
        with open(os.path.join(commit.segment_dir(self.home, only.commit_id), commit.FTS), "r+b") as f:
            f.write(b"damage")
        with self.assertRaises(CommitFailed) as cm:
            commit.rollback(self.home, self.conn)
        self.assertIn("do not verify", str(cm.exception))

    def test_rollback_refuses_a_commit_built_under_visibility_rules_that_no_longer_hold(self):
        """If the process dies after an activation and before the old history is cleaned, the retired commit is still on disk: rolling back to it must
        not bring back what the new rules hide (the same rule that refuses to activate a commit assembled under old rules)."""
        self.seed_docs()
        self.build()
        self.add("alpha.md", "# Alpha\n\nsecond\n")
        self.build()                                                                  # the commit before it is retired and its files are kept
        self.conn.execute("UPDATE collections SET profile_json=? WHERE collection_id=?", (Profile(agent_visible=True).to_json(), self.cid))
        with mock.patch.object(commit, "_after_publish", return_value=([], [])):             # cut off right after the activation
            third = self.build()
        with self.assertRaises(CommitFailed) as cm:
            commit.rollback(self.home, self.conn)
        self.assertIn("no longer hold", str(cm.exception))
        self.assertEqual(commit.active_commit_id(self.conn), third.commit_id)

    def test_a_truncated_active_index_is_refused_by_the_cheap_check(self):
        """Truncation changes the size, so serving can notice without hashing the file; it must refuse rather than read a broken index."""
        self.seed_docs()
        r = self.build()
        with open(os.path.join(commit.segment_dir(self.home, r.commit_id), commit.FTS), "r+b") as f:
            f.truncate(100)
        with self.assertRaises(ArtifactsMissing) as cm:
            commit.active_artifacts(self.home, self.conn)
        self.assertIn("wrong size", str(cm.exception))

    def test_bit_rot_is_found_by_the_full_check_not_the_cheap_one(self):
        self.seed_docs()
        r = self.build()
        p = os.path.join(commit.segment_dir(self.home, r.commit_id), commit.FTS)
        with open(p, "r+b") as f:
            f.seek(2000)
            b = f.read(1)
            f.seek(2000)
            f.write(bytes([b[0] ^ 1]))
        commit.active_artifacts(self.home, self.conn)                                   # same size: the cheap check passes
        self.assertTrue(commit.verify_artifacts(commit.segment_dir(self.home, r.commit_id))[1])


class TestGarbageCollection(World):
    def test_gc_keeps_active_and_recent_retired_and_removes_the_rest(self):
        self.seed_docs()
        made = [self.build().commit_id]
        for i in range(4):
            self.add("alpha.md", f"# Alpha\n\nversion {i}\n")
            made.append(self.build().commit_id)
        os.makedirs(os.path.join(self.home.sub("staging"), "cm_orphanorphanorphanorph"))
        os.makedirs(os.path.join(self.home.sub("segments"), "cm_unknownunknownunknownu"))
        removed = commit.gc(self.home, self.conn, keep_retired=2)
        self.assertIn("staging/cm_orphanorphanorphanorph", removed)
        self.assertIn("segments/cm_unknownunknownunknownu", removed)
        left = set(os.listdir(self.home.sub("segments")))
        self.assertEqual(left, set(made[-3:]))                                           # active plus the two most recent retired
        commit.active_artifacts(self.home, self.conn)


if __name__ == "__main__":
    unittest.main()
