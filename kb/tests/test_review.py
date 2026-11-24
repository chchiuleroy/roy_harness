"""S2a: proposals, approvals and what applying one does (T-D1 deletion needs approval; Gate 1 decisions 3 and 6; failure F20 stale approval).
Each test names the failure it guards: a change that happens without the user's approval, an approval applied to content that moved on, or a
rule that only the Python code (and not the database) enforces."""
import json
import os
import sqlite3
import unittest

from kb import commit, db, review
from kb.errors import ReviewRefused, StaleProposal

from .support import approved_proposal, tmpdir
from .world import World

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "kb")


class TestMigration4(unittest.TestCase):
    def test_upgrading_a_v3_database_keeps_its_data_and_gains_the_review_rules(self):
        """Existing knowledge must survive the first migration that adds enforcement."""
        d = tmpdir(self)
        path = os.path.join(d, "k.db")
        self.assertEqual(db.migrate(path, db.MIGRATIONS[:3]), [1, 2, 3])
        c = db.connect(path)
        c.execute("INSERT INTO collections(collection_id,name,profile_json,created_at) VALUES ('coll_aaaaaaaaaaaaaaaaaaaa','x','{}','t')")
        c.close()
        self.assertEqual(db.migrate(path, backup_dir=os.path.join(d, "bk")), [m.version for m in db.MIGRATIONS[3:]])        # 4 and everything after it
        c = db.connect(path)
        self.addCleanup(c.close)
        self.assertEqual(c.execute("SELECT count(*) FROM collections").fetchone()[0], 1)
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertTrue({"tombstones_need_approval", "approvals_only_pending", "proposals_state_machine",
                         "statements_need_approval_update", "statements_need_approval_insert"} <= names)
        self.assertEqual(c.execute("SELECT name FROM sqlite_master WHERE name='chunk_memo'").fetchone()[0], "chunk_memo")


class TestDatabaseEnforcesReview(World):
    """T-D1 at the database level: even code that bypasses review.py cannot hide, approve out of order or commit unapproved content."""

    def setUp(self):
        super().setUp()
        self.seed_docs()
        self.build()

    def tombstone(self, proposal_id):
        self.conn.execute("INSERT INTO tombstones(tombstone_id,target_id,level,proposal_id,at) VALUES ('tomb_zzzzzzzzzzzzzzzzzzzz',?, 'a',?, 't')",
                          (self.a.source_id, proposal_id))

    def test_a_tombstone_needs_an_approved_delete_proposal(self):
        self.conn.execute("INSERT INTO proposals(proposal_id,kind,diff_json,requested_by,state,created_at) VALUES ('pr_pend','delete','{}','roy','pending','t')")
        for pid in ("pr_missing", "pr_pend"):
            with self.assertRaises(sqlite3.IntegrityError, msg=pid):
                self.tombstone(pid)

    def test_an_approved_proposal_of_another_kind_cannot_hide_anything(self):
        """Everything else is right (approved, names this target and level): only the KIND is wrong, so only the kind rule can refuse it."""
        for kind in ("metadata", "ai_edit", "patch"):
            approved_proposal(self.conn, f"pr_{kind}", kind=kind, diff=json.dumps({"target_id": self.a.source_id, "level": "a"}))
            with self.assertRaises(sqlite3.IntegrityError, msg=kind):
                self.tombstone(f"pr_{kind}")

    def test_a_proposal_whose_state_says_approved_but_has_no_approval_row_cannot_hide_anything(self):
        """The state column alone is not an approval: someone must have approved. Everything else is right (kind, target, level), so only the
        missing approval row can refuse it."""
        self.conn.execute("INSERT INTO proposals(proposal_id,kind,diff_json,requested_by,state,created_at) VALUES ('pr_fake','delete',?,'roy','approved','t')",
                          (json.dumps({"target_id": self.a.source_id, "level": "a"}),))
        with self.assertRaises(sqlite3.IntegrityError):
            self.tombstone("pr_fake")

    def delete_diff(self, target, level="a"):
        return json.dumps({"target_id": target, "level": level})

    def test_an_approved_delete_proposal_can(self):
        approved_proposal(self.conn, "pr_ok", kind="delete", diff=self.delete_diff(self.a.source_id))
        self.tombstone("pr_ok")
        self.assertEqual(self.one("SELECT count(*) FROM tombstones")[0], 1)

    def test_an_approval_is_for_the_target_and_level_it_names_and_cannot_be_reused(self):
        """The approval of 'hide alpha at level a' must not hide beta, nor hide alpha harder than the user agreed. (Found in the S2 review: the trigger
        checked only that SOME delete proposal was approved.)"""
        approved_proposal(self.conn, "pr_alpha", kind="delete", diff=self.delete_diff(self.a.source_id, "a"))
        for target, level in ((self.b.source_id, "a"), (self.a.source_id, "b")):
            with self.assertRaises(sqlite3.IntegrityError, msg=f"{target} {level}"):
                self.conn.execute("INSERT INTO tombstones(tombstone_id,target_id,level,proposal_id,at) VALUES (?,?,?, 'pr_alpha','t')",
                                  (f"tomb_{level}{target[4:12]}", target, level))

    def test_level_c_does_not_exist_even_with_an_approval_that_names_it(self):
        approved_proposal(self.conn, "pr_c", kind="delete", diff=self.delete_diff(self.a.source_id, "c"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO tombstones(tombstone_id,target_id,level,proposal_id,at) VALUES ('tomb_cccccccccccccccccccc',?, 'c','pr_c','t')",
                              (self.a.source_id,))

    def test_an_approved_proposal_cannot_be_edited_afterwards(self):
        """What was approved is what gets applied: the difference, the kind and the base commit are frozen; a decided proposal's difference can
        only be blanked."""
        approved_proposal(self.conn, "pr_edit", kind="delete", diff=self.delete_diff(self.a.source_id))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE proposals SET diff_json=? WHERE proposal_id='pr_edit'", (self.delete_diff(self.b.source_id),))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE proposals SET kind='metadata' WHERE proposal_id='pr_edit'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE proposals SET requested_by='someone else' WHERE proposal_id='pr_edit'")
        self.conn.execute("INSERT INTO proposals(proposal_id,kind,diff_json,requested_by,state,created_at) VALUES ('pr_pen','delete','{}','roy','pending','t')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE proposals SET diff_json='{\"x\": 1}' WHERE proposal_id='pr_pen'")           # not even while pending
        self.conn.execute("UPDATE proposals SET state='applied' WHERE proposal_id='pr_edit'")
        self.conn.execute("UPDATE proposals SET diff_json='{\"purged\": true}' WHERE proposal_id='pr_edit'")      # a decided one may be blanked

    def test_only_a_pending_proposal_can_be_approved(self):
        for state in ("rejected", "stale"):
            self.conn.execute("INSERT INTO proposals(proposal_id,kind,diff_json,requested_by,state,created_at) VALUES (?,?, '{}','roy',?, 't')",
                              (f"pr_{state}", "delete", state))
            with self.assertRaises(sqlite3.IntegrityError, msg=state):
                self.conn.execute("INSERT INTO approvals(approval_id,proposal_id,approved_by,at) VALUES (?,?,'roy','t')", (f"ap_{state}", f"pr_{state}"))

    def test_proposal_states_move_along_legal_paths_only(self):
        self.conn.execute("INSERT INTO proposals(proposal_id,kind,diff_json,requested_by,state,created_at) VALUES ('pr_s','delete','{}','roy','pending','t')")
        for bad in ("applied", "pending"):                      # pending -> applied skips the approval; pending -> pending is not a move
            if bad == "pending":
                continue
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute("UPDATE proposals SET state=? WHERE proposal_id='pr_s'", (bad,))
        self.conn.execute("UPDATE proposals SET state='rejected' WHERE proposal_id='pr_s'")
        for back in ("pending", "approved", "applied"):
            with self.assertRaises(sqlite3.IntegrityError, msg=back):
                self.conn.execute("UPDATE proposals SET state=? WHERE proposal_id='pr_s'", (back,))

    def test_a_human_or_ai_statement_cannot_be_committed_without_an_approved_proposal(self):
        uv = self.uvs(self.a.source_id)[0]
        for origin in ("human", "ai"):
            sid = f"st_{origin}aaaaaaaaaaaaaaaaaaaaa"
            self.conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,state) VALUES (?,?,?,?,?, '{}', 'candidate')",
                              (sid, uv, "note", '"x"', origin))
            with self.assertRaises(sqlite3.IntegrityError, msg=origin):
                self.conn.execute("UPDATE statements SET state='committed' WHERE statement_id=?", (sid,))
            with self.assertRaises(sqlite3.IntegrityError, msg=origin + " insert"):
                self.conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,state) VALUES (?,?,?,?,?, '{}', 'committed')",
                                  (sid + "b", uv, "note", '"y"', origin))

    def test_a_statement_cannot_borrow_a_proposal_of_the_wrong_kind_or_one_it_does_not_name(self):
        """Found in the S2 review: an approved DELETE proposal could vouch for an AI statement. A statement is committed only with an approved
        proposal of a kind that fits its origin (ai -> ai_edit, human -> metadata or patch) and that the statement names in its evidence."""
        uv = self.uvs(self.a.source_id)[0]
        approved_proposal(self.conn, "pr_del", kind="delete", diff=json.dumps({"target_id": self.a.source_id, "level": "a"}))
        approved_proposal(self.conn, "pr_meta", kind="metadata")
        approved_proposal(self.conn, "pr_ai", kind="ai_edit")
        bad = [("human", "pr_del", {"proposal_id": "pr_del"}), ("ai", "pr_meta", {"proposal_id": "pr_meta"}), ("human", "pr_ai", {"proposal_id": "pr_ai"}),
               ("ai", "pr_del", {"proposal_id": "pr_del"}), ("human", "pr_meta", {}), ("human", "pr_meta", {"proposal_id": "pr_ai"})]
        for n, (origin, pid, ev) in enumerate(bad):
            sid = f"st_bad{n}aaaaaaaaaaaaaaaaaaaa"
            self.conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,state,proposal_id) VALUES (?,?,?,?,?,?, 'candidate', ?)",
                              (sid, uv, "note", f'"x{n}"', origin, json.dumps(ev), pid))
            with self.assertRaises(sqlite3.IntegrityError, msg=str(bad[n])):
                self.conn.execute("UPDATE statements SET state='committed' WHERE statement_id=?", (sid,))
        self.conn.execute("INSERT INTO proposals(proposal_id,kind,diff_json,requested_by,state,created_at) VALUES ('pr_norow','metadata','{}','roy','approved','t')")
        sid = "st_norowaaaaaaaaaaaaaaaaaa"                           # a proposal whose state says approved but which nobody approved
        self.conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,state,proposal_id) VALUES (?,?,?,?,?,?, 'candidate', 'pr_norow')",
                          (sid, uv, "note", '"nr"', "human", json.dumps({"proposal_id": "pr_norow"})))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE statements SET state='committed' WHERE statement_id=?", (sid,))
        good = [("human", "pr_meta"), ("ai", "pr_ai")]
        for n, (origin, pid) in enumerate(good):
            sid = f"st_good{n}aaaaaaaaaaaaaaaaaaa"
            self.conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,state,proposal_id) VALUES (?,?,?,?,?,?, 'candidate', ?)",
                              (sid, uv, "note", f'"g{n}"', origin, json.dumps({"proposal_id": pid}), pid))
            self.conn.execute("UPDATE statements SET state='committed' WHERE statement_id=?", (sid,))

    def test_the_proposal_a_statement_came_from_is_never_changed_afterwards(self):
        """Round 2 of the S2 review: the link could be rewritten after the statement was committed, so a later commit would disagree with the
        earlier one about whether it was ever approved. It is written once, with the statement, and never again."""
        uv = self.uvs(self.a.source_id)[0]
        approved_proposal(self.conn, "pr_meta", kind="metadata")
        approved_proposal(self.conn, "pr_other", kind="metadata")
        sid = "st_linkedaaaaaaaaaaaaaaaaa"
        self.conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,state,proposal_id) VALUES (?,?,?,?,?,?, 'candidate', 'pr_meta')",
                          (sid, uv, "note", '"x"', "human", json.dumps({"proposal_id": "pr_meta"})))
        for new in ("pr_other", None):
            with self.assertRaises(sqlite3.IntegrityError, msg=str(new)):              # not even while it is still a candidate
                self.conn.execute("UPDATE statements SET proposal_id=? WHERE statement_id=?", (new, sid))
        self.conn.execute("UPDATE statements SET state='committed' WHERE statement_id=?", (sid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE statements SET proposal_id=NULL WHERE statement_id=?", (sid,))
        self.assertIn(sid, commit.approval_sets(self.conn)[0])

    def test_assembly_also_ignores_a_statement_whose_approval_does_not_fit(self):
        """The same rule one layer up: the commit does not count such a statement as approved, so it is not a member (and cannot reach the trigger)."""
        uv = self.uvs(self.a.source_id)[0]
        approved_proposal(self.conn, "pr_del", kind="delete", diff=json.dumps({"target_id": self.a.source_id, "level": "a"}))
        self.conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,state,proposal_id) VALUES "
                          "('st_borrowedaaaaaaaaaaaaaa',?, 'note', '\"x\"', 'human', ?, 'candidate', 'pr_del')", (uv, json.dumps({"proposal_id": "pr_del"})))
        self.assertNotIn("st_borrowedaaaaaaaaaaaaaa", commit.approval_sets(self.conn)[0])
        # right kind, approved, but the statement does not name its proposal in its own evidence: only the binding can refuse it
        approved_proposal(self.conn, "pr_meta", kind="metadata")
        self.conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,state,proposal_id) VALUES "
                          "('st_unboundaaaaaaaaaaaaaaa',?, 'note', '\"y\"', 'human', '{}', 'candidate', 'pr_meta')", (uv,))
        self.conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,state,proposal_id) VALUES "
                          "('st_boundaaaaaaaaaaaaaaaaa',?, 'note', '\"z\"', 'human', ?, 'candidate', 'pr_meta')", (uv, json.dumps({"proposal_id": "pr_meta"})))
        approved = commit.approval_sets(self.conn)[0]
        self.assertNotIn("st_unboundaaaaaaaaaaaaaaa", approved)
        self.assertIn("st_boundaaaaaaaaaaaaaaaaa", approved)
        self.add("more.md", "# More\n\nforce a new commit\n")
        r = self.build()
        n = self.one("SELECT count(*) FROM commit_statements WHERE commit_id=? AND statement_id='st_borrowedaaaaaaaaaaaaaa'", r.commit_id)[0]
        self.assertEqual(n, 0)

    def test_only_review_py_writes_tombstones(self):
        """T-D1, the other half: there is no second deletion path in the code (the database rule above is the backstop)."""
        writers = []
        for name in sorted(os.listdir(SRC_DIR)):
            if name.endswith(".py"):
                with open(os.path.join(SRC_DIR, name), encoding="utf-8") as f:
                    if "insert into tombstones" in f.read().lower():
                        writers.append(name)
        self.assertEqual(writers, ["review.py"])


class TestLifecycle(World):
    def setUp(self):
        super().setUp()
        self.seed_docs()
        self.build()
        self.title_stmt = lambda: self.conn.execute("SELECT count(*) FROM statements WHERE origin='human'").fetchone()[0]

    def meta(self, value="Falcon Notes"):
        return {"statements": [{"subject_id": self.a.source_id, "key": "title", "value": value}]}

    def test_a_proposal_changes_nothing_until_it_is_approved_and_applied(self):
        """Gate 1 decision 6: the knowledge changes only after approval. Creating and even approving must not touch statements or commits."""
        before = self.table_hash("statements", "commits", "commit_statements", "tombstones", "patches")
        pid = review.create(self.conn, "metadata", self.meta(), "roy")
        self.assertEqual(self.table_hash("statements", "commits", "commit_statements", "tombstones", "patches"), before)
        review.approve(self.conn, pid, "roy")
        self.assertEqual(self.table_hash("statements", "commits", "commit_statements", "tombstones", "patches"), before)
        review.apply(self.conn, pid)
        self.assertEqual(self.title_stmt(), 1)

    def test_nothing_is_visible_before_the_next_commit(self):
        self.decide("metadata", self.meta())
        self.assertEqual(self.displayed(self.a.source_id, "title")[1], "native")
        self.build()
        self.assertEqual(self.displayed(self.a.source_id, "title"), ("Falcon Notes", "human"))

    def test_who_asked_who_approved_and_against_which_commit_are_recorded(self):
        pid = review.create(self.conn, "metadata", self.meta(), "小c")
        review.approve(self.conn, pid, "Roy")
        row = self.one("SELECT requested_by, base_commit_id, state FROM proposals WHERE proposal_id=?", pid)
        self.assertEqual(row, ("小c", commit.active_commit_id(self.conn), "approved"))
        self.assertEqual(self.one("SELECT approved_by FROM approvals WHERE proposal_id=?", pid)[0], "Roy")

    def test_an_unapproved_statement_never_enters_a_commit(self):
        """A pending proposal's statement is not even a candidate member: the next commit must not carry it."""
        pid = review.create(self.conn, "metadata", self.meta("Never approved"), "roy")
        review.reject(self.conn, pid, "roy")
        self.add("extra.md", "# Extra\n\nforce a new commit\n")
        r = self.build()
        n = self.one("SELECT count(*) FROM commit_statements cs JOIN statements s ON s.statement_id = cs.statement_id WHERE cs.commit_id=? AND s.origin='human'", r.commit_id)[0]
        self.assertEqual(n, 0)

    def test_apply_needs_an_approval_and_cannot_run_twice(self):
        pid = review.create(self.conn, "metadata", self.meta(), "roy")
        with self.assertRaises(ReviewRefused):
            review.apply(self.conn, pid)
        review.approve(self.conn, pid, "roy")
        review.apply(self.conn, pid)
        with self.assertRaises(ReviewRefused):
            review.apply(self.conn, pid)
        with self.assertRaises(ReviewRefused):
            review.approve(self.conn, pid, "roy")

    def test_a_rejected_proposal_cannot_be_approved(self):
        pid = review.create(self.conn, "metadata", self.meta(), "roy")
        review.reject(self.conn, pid, "roy")
        with self.assertRaises(ReviewRefused):
            review.approve(self.conn, pid, "roy")

    def test_a_proposal_made_against_an_older_commit_is_stale_and_not_approvable(self):
        """F20: the user approves a difference that was computed on content that has since changed. It must not be applied silently."""
        pid = review.create(self.conn, "metadata", self.meta(), "roy")
        self.add("later.md", "# Later\n\nsomething new\n")
        self.build()
        with self.assertRaises(StaleProposal):
            review.approve(self.conn, pid, "roy")
        self.assertEqual(self.one("SELECT state FROM proposals WHERE proposal_id=?", pid)[0], "stale")
        self.assertEqual(self.one("SELECT count(*) FROM approvals WHERE proposal_id=?", pid)[0], 0)
        self.assertEqual(self.title_stmt(), 0)

    def test_a_proposal_approved_and_then_overtaken_by_a_commit_is_stale_at_apply(self):
        pid = review.create(self.conn, "metadata", self.meta(), "roy")
        review.approve(self.conn, pid, "roy")
        self.add("later.md", "# Later\n\nsomething new\n")
        self.build()
        with self.assertRaises(StaleProposal):
            review.apply(self.conn, pid)
        self.assertEqual(self.one("SELECT state FROM proposals WHERE proposal_id=?", pid)[0], "stale")
        self.assertEqual(self.title_stmt(), 0)

    def test_checks_are_made_after_the_write_lock_is_taken(self):
        """Found in the S2 review: reading the proposal and the active commit BEFORE the transaction left a gap in which another writer could move
        the knowledge on. Every read that decides the outcome must happen inside the transaction."""
        seen = []
        real = review._row
        review._row = lambda conn, pid: (seen.append(conn.in_transaction), real(conn, pid))[1]
        self.addCleanup(setattr, review, "_row", real)
        pid = review.create(self.conn, "metadata", self.meta(), "roy")
        review.approve(self.conn, pid, "roy")
        review.apply(self.conn, pid)
        self.assertEqual(seen, [True, True])

    def test_a_failure_while_applying_writes_nothing(self):
        """Apply is one transaction: a proposal whose second statement is refused must not leave its first one behind."""
        diff = {"statements": [{"subject_id": self.a.source_id, "key": "title", "value": "First"},
                               {"subject_id": self.b.source_id, "key": "title", "value": "Second"}]}
        pid = review.create(self.conn, "metadata", diff, "roy")
        review.approve(self.conn, pid, "roy")
        real = review.ids.statement_id
        calls = []

        def boom(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("disk full")
            return real(*a, **k)
        review.ids.statement_id = boom
        self.addCleanup(setattr, review.ids, "statement_id", real)
        with self.assertRaises(RuntimeError):
            review.apply(self.conn, pid)
        self.assertEqual(self.title_stmt(), 0)
        self.assertEqual(self.one("SELECT state FROM proposals WHERE proposal_id=?", pid)[0], "approved")

    def test_proposal_kinds_that_are_not_available_are_refused_outright(self):
        for kind, diff in (("purge", {"target_id": self.a.source_id}), ("merge_tag", {"from": "x", "to": "y"}), ("nonsense", {})):
            with self.assertRaises(ReviewRefused, msg=kind):
                review.create(self.conn, kind, diff, "roy")

    def test_bad_diffs_are_refused_when_proposed(self):
        bad = [
            {"statements": []},
            {"statements": [{"subject_id": "src_aaaaaaaaaaaaaaaaaaaaaaaa", "key": "title", "value": "x"}]},            # no such source
            {"statements": [{"subject_id": self.a.source_id, "key": "origin", "value": "x"}]},                       # key not allowed
            {"statements": [{"subject_id": self.a.source_id, "key": "title", "value": "  "}]},
            {"statements": [{"subject_id": self.a.source_id, "key": "title", "value": "x" * 20001}]},
            {"statements": [{"subject_id": self.a.source_id, "key": "title", "value": "x", "supersedes": "st_aaaaaaaaaaaaaaaaaaaaaaaa"}]},
            {"statements": [{"subject_id": self.a.source_id, "key": "title", "value": "x"}] * 51},
        ]
        for diff in bad:
            with self.assertRaises(ReviewRefused):
                review.create(self.conn, "metadata", diff, "roy")
        with self.assertRaises(ReviewRefused):
            review.create(self.conn, "metadata", self.meta(), "  ")

    def test_audit_records_identifiers_never_the_text_of_a_proposal(self):
        self.decide("metadata", self.meta("zzcanaryaudit1"))
        self.assertFalse(any("zzcanaryaudit1" in json.dumps(e) for e in self.conn.execute("SELECT * FROM audit_events").fetchall()))
        types = {r[0] for r in self.conn.execute("SELECT type FROM audit_events")}
        self.assertTrue({"proposal_created", "proposal_approved", "proposal_applied"} <= types)


class TestRetention(World):
    def setUp(self):
        super().setUp()
        self.seed_docs()
        self.build()

    def proposal(self, state, decided):
        pid = review.create(self.conn, "metadata", {"statements": [{"subject_id": self.a.source_id, "key": "note", "value": "zzcanaryret1"}]}, "roy",
                            now="2026-01-01T00:00:00Z")
        if state == "rejected":
            review.reject(self.conn, pid, "roy", now=decided)
        elif state in ("approved", "applied"):
            review.approve(self.conn, pid, "roy", now=decided)
            if state == "applied":
                review.apply(self.conn, pid, now=decided)
        return pid

    def diff_of(self, pid):
        return self.one("SELECT diff_json FROM proposals WHERE proposal_id=?", pid)[0]

    def test_decided_proposals_lose_their_diff_after_the_retention_period_and_undecided_ones_never_do(self):
        old, fresh = "2026-01-02T00:00:00Z", "2026-02-20T00:00:00Z"
        rej_old = self.proposal("rejected", old)
        app_old = self.proposal("applied", old)
        rej_new = self.proposal("rejected", fresh)
        pending = self.proposal("pending", None)
        approved_unapplied = self.proposal("approved", old)
        n = review.purge_expired(self.conn, 30, now="2026-03-01T00:00:00Z")
        self.assertEqual(n, 2)
        for pid in (rej_old, app_old):
            self.assertNotIn("zzcanaryret1", self.diff_of(pid))
        for pid in (rej_new, pending, approved_unapplied):
            self.assertIn("zzcanaryret1", self.diff_of(pid))
        self.assertEqual(review.purge_expired(self.conn, 30, now="2026-03-01T00:00:00Z"), 0)            # idempotent

    def test_the_boundary_is_exact_and_does_not_depend_on_the_time_zone_of_the_machine(self):
        """Decided exactly the retention period ago is expired; one second later is not. (An earlier version read the UTC text as local time and was
        off by the machine's offset from UTC, which dates months apart cannot reveal.)"""
        exactly = self.proposal("rejected", "2026-02-01T12:00:00Z")
        later = self.proposal("rejected", "2026-02-01T12:00:01Z")
        review.purge_expired(self.conn, 30, now="2026-03-03T12:00:00Z")
        self.assertNotIn("zzcanaryret1", self.diff_of(exactly))
        self.assertIn("zzcanaryret1", self.diff_of(later))

    def test_a_purged_proposal_still_describes_itself_honestly(self):
        pid = self.proposal("rejected", "2026-01-02T00:00:00Z")
        review.purge_expired(self.conn, 30, now="2026-03-01T00:00:00Z")
        self.assertIn("removed after the retention period", "\n".join(review.describe(self.conn, pid)))


if __name__ == "__main__":
    unittest.main()
