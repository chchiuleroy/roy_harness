"""Slice 2, checkpoint S-A: the evaluation ledger. Every move a candidate can make between
"waiting for the model" and "decided", and every attempt to apply an answer that does not match.

Each test's docstring says WHY the behaviour matters.
"""
import os
import sqlite3
import tempfile
import unittest

import superagent_support as sup  # noqa: F401
from superagent.gate import Policy, Rule
from superagent.model import Authorized, Decision, Strength, TicketState, Trigger, Verdict
from superagent.store import EVAL_MAX_ATTEMPTS, StaleBatch, Store

AUTO = Policy(allowed_senders=frozenset({"alice"}), accept_mode="auto", rules=(Rule("export-bug", "export fails"),))
OBSERVE = Policy(allowed_senders=frozenset({"alice"}), accept_mode="observe", rules=AUTO.rules)


def trig(ext, thread="t", text="maybe a request"):
    return Trigger("file:inbox", ext, thread, "bob", text, False, 1.0)


# What the model said, exactly: (rule name or None, reason). The store decides what follows.
def accept(rule="export-bug"):
    return (rule, "ok")


def ignore():
    return (None, "no")


def unavailable():
    return ("no-such-rule", "x")


class Ledger(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.s = Store(self.home).open()
        self.calls = []

    def tearDown(self):
        self.s.close()
        self._tmp.cleanup()

    def paths(self, i):
        return (os.path.join(self.home, f"w{i}"), os.path.join(self.home, f"s{i}.json"),
                os.path.join(self.home, f"w{i}"))

    def candidate(self, ext):
        with self.s.tx():
            tid = self.s.add_trigger(trig(ext))
            self.s.enqueue_eval(tid)
        return tid

    def batch(self, ids, text="batch text"):
        return self.s.create_eval_batch(ids, text, 1, 100.0)

    def finish(self, bid, answers, policy=AUTO):
        return self.s.finish_eval(bid, answers, policy, self.paths,
                                  lambda trig, tid: self.calls.append((trig["id"], tid)))

    def decisions(self):
        return {d["trigger_id"]: d for d in self.s.decisions()}


class TestTransitions(Ledger):
    def test_pending_to_sent_to_done(self):
        a, b = self.candidate("a"), self.candidate("b")
        self.assertEqual([i["trigger_id"] for i in self.s.peek_eval_items(10)], [a, b])
        bid = self.batch([a, b], "the exact text")
        self.assertEqual(self.s.eval_batch(bid)["text"], "the exact text")
        self.assertEqual(self.s.eval_counts()["sent"], 2)
        self.assertEqual(self.s.peek_eval_items(10), [], "sent candidates must not be offered again")
        rep = self.finish(bid, {a: ignore(), b: ignore()})
        self.assertEqual(sorted(rep["done"]), [a, b])
        self.assertEqual(self.s.eval_batch(bid)["state"], "done")
        self.assertEqual(self.s.eval_counts()["done"], 2)

    def test_accept_opens_a_ticket_whose_mandate_names_the_rule(self):
        """The mandate says why the work may be done. A rule name only in the model's own words
        would not be evidence, so it is a structured field of the immutable mandate."""
        a = self.candidate("a")
        bid = self.batch([a])
        rep = self.finish(bid, {a: accept("export-bug")})
        tid = rep["accepted"][0]
        m = self.s.mandate(tid)
        self.assertEqual((m["strength"], m["matched_rules"]), ("inferred", ["export-bug"]))
        self.assertEqual(self.calls, [(a, tid)], "the caller's follow-up step did not run")
        self.assertEqual(self.decisions()[a]["ticket_id"], tid)

    def test_observe_mode_records_would_accept_and_opens_nothing(self):
        """The same answer that opens a ticket under `auto` only leaves a note under `observe`; and the
        mode is applied by the store when it applies the answer, not trusted from the caller."""
        a = self.candidate("a")
        bid = self.batch([a])
        rep = self.finish(bid, {a: accept()}, policy=OBSERVE)
        self.assertEqual((rep["accepted"], self.s.tickets()), ([], []))
        d = self.decisions()[a]
        self.assertEqual((d["verdict"], d["would_accept"]), ("ignore", 1))
        self.assertEqual(self.calls, [])

    def test_unknown_rule_is_recorded_as_unavailable_and_final(self):
        a = self.candidate("a")
        bid = self.batch([a])
        rep = self.finish(bid, {a: unavailable()})
        self.assertEqual(rep["unavailable"], [a])
        self.assertEqual(self.s.eval_item(a)["state"], "unavailable")
        self.assertEqual(self.decisions()[a]["verdict"], "unavailable")

    def test_only_one_batch_in_flight(self):
        """One outstanding question to the model, so a reply can only belong to one batch."""
        a, b = self.candidate("a"), self.candidate("b")
        self.batch([a])
        with self.assertRaises(StaleBatch):
            self.batch([b])

    def test_a_batch_takes_only_waiting_candidates(self):
        a = self.candidate("a")
        self.batch([a])
        with self.assertRaises(StaleBatch):
            self.s.create_eval_batch([a], "again", 1, 1.0)
        with self.assertRaises(StaleBatch):
            self.s.create_eval_batch([], "empty", 1, 1.0)


class TestApplyIsGuarded(Ledger):
    def test_the_same_answer_applied_twice_opens_one_ticket(self):
        """Found by review: nothing stopped a reply being applied twice. The second try must be
        refused and must not open a second ticket."""
        a = self.candidate("a")
        bid = self.batch([a])
        self.finish(bid, {a: accept()})
        with self.assertRaises(StaleBatch):
            self.finish(bid, {a: accept()})
        self.assertEqual(len(self.s.tickets()), 1)
        self.assertEqual(len(self.calls), 1)

    def test_an_empty_answer_for_a_finished_batch_must_not_put_its_candidates_back_in_line(self):
        """Without the 'batch is still in flight' check, an empty answer would count every finished
        candidate of a done batch as unanswered and push it back to pending."""
        a = self.candidate("a")
        bid = self.batch([a])
        self.finish(bid, {a: ignore()})
        with self.assertRaises(StaleBatch):
            self.finish(bid, {})
        self.assertEqual(self.s.eval_item(a)["state"], "done")
        self.assertEqual(self.s.eval_item(a)["attempts"], 0)

    def test_a_trigger_that_is_not_in_the_batch_rejects_the_whole_answer(self):
        """The model names trigger ids; a wrong one must not be applied, and must not take the valid
        ones in the same answer down half-way."""
        a, b = self.candidate("a"), self.candidate("b")
        bid = self.batch([a])
        with self.assertRaises(StaleBatch):
            self.finish(bid, {a: accept(), b: accept()})
        self.assertEqual(self.s.tickets(), [])
        self.assertEqual(self.decisions(), {})
        self.assertEqual(self.s.eval_item(a)["state"], "sent")
        self.assertEqual(self.s.eval_item(b)["state"], "pending")

    def test_a_trigger_that_already_has_a_decision_is_refused(self):
        a = self.candidate("a")
        bid = self.batch([a])
        self.s.record_decision(a, Decision(Verdict.IGNORE, Strength.INFERRED, "decided elsewhere"))
        with self.assertRaises(StaleBatch):
            self.finish(bid, {a: accept()})
        self.assertEqual(self.s.tickets(), [])

    def test_one_decision_per_trigger_even_if_everything_else_fails(self):
        """The database itself is the last line: two decisions for one trigger cannot exist."""
        a = self.candidate("a")
        d = Decision(Verdict.IGNORE, Strength.INFERRED, "x")
        self.s.record_decision(a, d)
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.record_decision(a, d)

    def test_a_decision_cannot_be_handed_in_only_what_the_model_said(self):
        """Found by review: an `Authorized` object could be built by anyone, so checking its type proved
        nothing. The store now takes the model's raw answer and runs the authorisation itself; a ready-made
        decision of any kind is refused."""
        a = self.candidate("a")
        bid = self.batch([a])
        forged = (Decision(Verdict.ACCEPT, Strength.INFERRED, "forged"),
                  Authorized(Decision(Verdict.ACCEPT, Strength.INFERRED, "forged"), ("export-bug",)),
                  ("export-bug",), ("export-bug", 5), (5, "x"), "export-bug", None)
        for bad in forged:
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(TypeError):
                    self.finish(bid, {a: bad})
        self.assertEqual(self.s.tickets(), [])
        self.assertEqual(self.decisions(), {})

    def test_the_rule_must_exist_in_the_policy_the_store_is_given(self):
        a = self.candidate("a")
        bid = self.batch([a])
        rep = self.finish(bid, {a: ("export-bug", "x")}, policy=Policy(accept_mode="auto", rules=()))
        self.assertEqual(rep["unavailable"], [a])
        self.assertEqual(self.s.tickets(), [])

    def test_an_answer_for_a_requeued_batch_is_stale(self):
        a = self.candidate("a")
        bid = self.batch([a])
        self.s.requeue_eval(bid, "died")
        with self.assertRaises(StaleBatch):
            self.finish(bid, {a: accept()})
        self.assertEqual(self.s.tickets(), [])


class TestUnanswered(Ledger):
    def test_a_candidate_the_model_keeps_skipping_ends_unavailable(self):
        """Found by review: a reply that was readable but always left out the same id would have
        had that id asked again for ever. Each round here answers a fresh neighbour and skips `a`."""
        a = self.candidate("a")
        for attempt in range(1, EVAL_MAX_ATTEMPTS + 1):
            other = self.candidate(f"other{attempt}")
            bid = self.batch([a, other])
            rep = self.finish(bid, {other: ignore()})
            if attempt < EVAL_MAX_ATTEMPTS:
                self.assertIn(a, rep["pending"], f"attempt {attempt}")
                self.assertEqual(self.s.eval_item(a)["attempts"], attempt)
            else:
                self.assertIn(a, rep["unavailable"])
        self.assertEqual(self.s.eval_item(a)["state"], "unavailable")
        self.assertEqual(self.decisions()[a]["verdict"], "unavailable")
        self.assertEqual(self.s.peek_eval_items(10), [])

    def test_requeue_counts_the_try_and_ends_unavailable_at_the_limit(self):
        a = self.candidate("a")
        for attempt in range(1, EVAL_MAX_ATTEMPTS + 1):
            bid = self.batch([a])
            self.assertEqual(self.s.requeue_eval(bid, "died"), [a])
            self.assertEqual(self.s.eval_batch(bid)["state"], "requeued")
        self.assertEqual(self.s.eval_item(a)["state"], "unavailable")
        self.assertEqual(self.decisions()[a]["verdict"], "unavailable")

    def test_requeue_of_a_finished_batch_changes_nothing(self):
        a = self.candidate("a")
        bid = self.batch([a])
        self.finish(bid, {a: ignore()})
        self.assertEqual(self.s.requeue_eval(bid, "late"), [])
        self.assertEqual(self.s.eval_item(a)["state"], "done")


class TestCrashConsistency(Ledger):
    def test_trigger_and_wait_marker_are_one_commit(self):
        """A crash between 'stored the message' and 'queued it for the model' would leave a message
        nobody will ever look at."""
        class Boom(Exception):
            pass
        with self.assertRaises(Boom):
            with self.s.tx():
                tid = self.s.add_trigger(trig("a"))
                self.s.enqueue_eval(tid)
                raise Boom()
        self.assertEqual(self.s.peek_eval_items(10), [])
        self.assertEqual(self.s._q("SELECT COUNT(*) AS n FROM trigger")[0]["n"], 0)

    def test_a_failing_follow_up_step_rolls_the_whole_answer_back(self):
        a = self.candidate("a")
        bid = self.batch([a])

        def boom(trig, tid):
            raise RuntimeError("cannot queue the first input")
        with self.assertRaises(RuntimeError):
            self.s.finish_eval(bid, {a: accept()}, AUTO, self.paths, boom)
        self.assertEqual(self.s.tickets(), [])
        self.assertEqual(self.decisions(), {})
        self.assertEqual(self.s.eval_batch(bid)["state"], "sent")
        self.assertEqual(self.s.eval_item(a)["state"], "sent")


class TestOlderHomes(unittest.TestCase):
    def test_a_slice_1_home_is_upgraded(self):
        """A home made before the evaluation ledger existed has no eval tables, no unique index on
        decisions and mandates without matched_rules. Opening it must add what is missing and leave
        the old data as it was."""
        from superagent import store as store_mod
        cut = store_mod.SCHEMA.index("CREATE UNIQUE INDEX IF NOT EXISTS decision_one_per_trigger")
        old_schema = store_mod.SCHEMA[:cut]
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(os.path.join(d, "state.sqlite"))
            db.executescript(old_schema)
            db.execute("INSERT INTO trigger(id, source, ext_id, thread, sender, text, addressed, ts) "
                       "VALUES(1,'s','e','t','alice','hi',1,1.0)")
            db.execute("INSERT INTO ticket(id, source, thread, state, created) VALUES(1,'s','t','open',1.0)")
            db.execute("INSERT INTO mandate(ticket_id, body) VALUES(1,'{\"ticket_id\": 1, \"root\": \"r\", "
                       "\"strength\": \"explicit\"}')")
            db.execute("INSERT INTO decision(trigger_id, verdict, strength, reason, would_accept, ticket_id, ts) "
                       "VALUES(1,'accept','explicit','explicit request',0,1,1.0)")
            db.commit()
            db.close()
            with Store(d) as s:
                tables = {r["name"] for r in s._q("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("eval_item", tables)
                self.assertIn("eval_batch", tables)
                idx = {r["name"] for r in s._q("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertIn("decision_one_per_trigger", idx)
                self.assertEqual(s.mandate(1)["strength"], "explicit")
                self.assertEqual(len(s.decisions()), 1)
                tid = s.add_trigger(trig("new"))
                s.enqueue_eval(tid)
                self.assertEqual(len(s.peek_eval_items(5)), 1)


if __name__ == "__main__":
    unittest.main()
