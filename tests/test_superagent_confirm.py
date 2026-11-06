"""The second look: a rule the model names is asked about once more, in a fresh session, and the candidate
is taken only if both answers name the same rule (`Policy.confirm_inferred`, on by default).

Each test's docstring says WHY the behaviour matters.
"""
import json
import os
import sqlite3
import tempfile
import unittest

import superagent_support as sup  # noqa: F401
from superagent import gate
from superagent import replay as rp
from superagent import store as store_mod
from superagent.gate import Policy, Rule, authorize, confirm, needs_confirmation
from superagent.measure import NULL, UNAVAILABLE
from superagent.model import Strength, Verdict
from superagent.store import EVAL_MAX_ATTEMPTS, Store
from test_superagent_eval_engine import Rig
from test_superagent_eval_store import Ledger, trig
from test_superagent_replay import ReplayCase, row

TWO_RULES = (Rule("export-bug", "export fails"), Rule("billing", "billing question"))


def policy(mode="auto", **kw):
    return Policy(allowed_senders=frozenset({"alice"}), accept_mode=mode, rules=TWO_RULES, **kw)


AUTO, OBSERVE = policy("auto"), policy("observe")


def first(rule="export-bug", why="first words"):
    return authorize(rule, why, Strength.INFERRED, AUTO)


class TestTheDecisionAfterTwoLooks(unittest.TestCase):
    def test_the_same_rule_twice_is_taken_in_auto_and_previewed_in_observe(self):
        """The whole point: two agreeing looks are what opens a ticket, and observe's `would_accept` says
        what auto would do, so it needs the same two looks."""
        d = confirm("export-bug", "one", "export-bug", "two", AUTO)
        self.assertEqual((d.decision.verdict, d.matched_rules), (Verdict.ACCEPT, ("export-bug",)))
        self.assertIn("confirmed twice", d.decision.reason)
        self.assertIn("one", d.decision.reason)
        self.assertIn("two", d.decision.reason)
        o = confirm("export-bug", "one", "export-bug", "two", OBSERVE).decision
        self.assertEqual((o.verdict, o.would_accept, o.model_rule), (Verdict.IGNORE, True, "export-bug"))

    def test_no_rule_or_another_rule_the_second_time_is_not_taken_and_says_what_the_model_said_last(self):
        """A rule that was named once and not repeated is a guess. The audit trail keeps the LAST thing the
        model said (so a person reading it sees the disagreement), and nothing is previewed as accepted."""
        for second, kept in ((None, ""), ("billing", "billing")):
            for pol in (AUTO, OBSERVE):
                with self.subTest(second=second, mode=pol.accept_mode):
                    a = confirm("export-bug", "one", second, "two", pol)
                    d = a.decision
                    self.assertEqual((d.verdict, d.would_accept, d.model_rule), (Verdict.IGNORE, False, kept))
                    self.assertEqual(a.matched_rules, ())
                    self.assertIn("export-bug", d.reason)

    def test_a_name_the_policy_does_not_know_is_unavailable_as_on_a_first_look(self):
        """The model's words are never trusted just because it is the second time it speaks."""
        d = confirm("export-bug", "one", "no-such-rule", "two", AUTO).decision
        self.assertEqual((d.verdict, d.reason), (Verdict.UNAVAILABLE, "unknown rule"))

    def test_the_second_look_is_cleaned_and_cut_like_any_text_from_the_model(self):
        long_ = "x" * 500
        d = confirm("export-bug", "a\x1b[31mb" + long_, "export-bug", long_, AUTO).decision
        self.assertLessEqual(len(d.reason), gate.RULE_MAX + 100)
        self.assertFalse(any(ord(c) < 32 for c in d.reason))

    def test_when_a_second_look_is_due(self):
        """Only a first answer that names a known rule, in a mode that can act on it, with the switch on."""
        self.assertTrue(needs_confirmation(first(), AUTO))
        self.assertTrue(needs_confirmation(authorize("export-bug", "w", Strength.INFERRED, OBSERVE), OBSERVE))
        self.assertFalse(needs_confirmation(authorize(None, "w", Strength.INFERRED, AUTO), AUTO), "no rule named")
        self.assertFalse(needs_confirmation(authorize("zzz", "w", Strength.INFERRED, AUTO), AUTO), "unknown name")
        off = policy("off")
        self.assertFalse(needs_confirmation(authorize("export-bug", "w", Strength.INFERRED, off), off))
        no = policy("auto", confirm_inferred=False)
        self.assertFalse(needs_confirmation(authorize("export-bug", "w", Strength.INFERRED, no), no))


class Looks(Ledger):
    """The ledger with a policy that has two rules and the second look on."""

    def finish(self, bid, answers, policy=AUTO):
        return super().finish(bid, answers, policy)

    def first_look(self, ext="a", answer=("export-bug", "first words"), policy=AUTO):
        tid = self.candidate(ext)
        rep = self.finish(self.batch([tid]), {tid: answer}, policy)
        return tid, rep

    def tickets_and_decisions(self):
        return len(self.s.tickets()), len(self.s.decisions())


class TestTheLedgerAfterTheFirstLook(Looks):
    def test_a_named_rule_opens_nothing_and_waits_for_a_second_look(self):
        """If the first answer were enough, the second look would be decoration."""
        tid, rep = self.first_look()
        self.assertEqual(rep["confirming"], [tid])
        self.assertEqual((rep["accepted"], rep["done"]), ([], []))
        self.assertEqual(self.tickets_and_decisions(), (0, 0))
        it = self.s.eval_item(tid)
        self.assertEqual((it["state"], it["phase"], it["first_rule"], it["first_reason"], it["batch_id"]),
                         ("pending", "confirm", "export-bug", "first words", None))
        self.assertEqual([i["phase"] for i in self.s.peek_eval_items(10)], ["confirm"])

    def test_no_rule_at_the_first_look_is_decided_at_once_as_before(self):
        tid = self.candidate("a")
        rep = self.finish(self.batch([tid]), {tid: (None, "no")})
        self.assertEqual((rep["done"], rep["confirming"]), ([tid], []))
        self.assertEqual(self.s.eval_item(tid)["phase"], "first")

    def test_the_second_look_gets_a_full_set_of_tries_of_its_own(self):
        """A candidate that needed retries for its first look must not arrive at the second one with the
        tries already used up."""
        tid = self.candidate("a")
        self.s.requeue_eval(self.batch([tid]), "slow")
        self.s.requeue_eval(self.batch([tid]), "slow")
        self.assertEqual(self.s.eval_item(tid)["attempts"], 2)
        self.finish(self.batch([tid]), {tid: ("export-bug", "w")})
        self.assertEqual(self.s.eval_item(tid)["attempts"], 0)

    def test_a_lone_surrogate_in_the_first_reason_cannot_crash_the_store(self):
        """The reason is kept for the second look, so it is cleaned before it is stored (as a decision's is)."""
        tid, _ = self.first_look(answer=("export-bug", "x\ud800y\x1b[31m"))
        self.assertNotIn("\ud800", self.s.eval_item(tid)["first_reason"])


class TestTheLedgerAfterTheSecondLook(Looks):
    def test_the_same_rule_opens_exactly_one_ticket_whose_mandate_names_it(self):
        tid, _ = self.first_look()
        rep = self.finish(self.batch([tid]), {tid: ("export-bug", "second words")})
        self.assertEqual((len(rep["accepted"]), rep["confirming"]), (1, []))
        d = self.decisions()[tid]
        self.assertEqual((d["verdict"], d["ticket_id"] is not None, d["model_rule"]), ("accept", True, "export-bug"))
        self.assertIn("first words", d["reason"])
        self.assertIn("second words", d["reason"])
        self.assertEqual(self.s.mandate(rep["accepted"][0])["matched_rules"], ["export-bug"])
        self.assertEqual(self.s.eval_item(tid)["state"], "done")

    def test_no_rule_or_another_rule_opens_nothing(self):
        for second, kept in ((None, ""), ("billing", "billing")):
            with self.subTest(second=second):
                tid, _ = self.first_look(ext=f"x{second}")
                rep = self.finish(self.batch([tid]), {tid: (second, "second words")})
                self.assertEqual((rep["accepted"], rep["done"]), ([], [tid]))
                d = self.decisions()[tid]
                self.assertEqual((d["verdict"], d["would_accept"], d["model_rule"], d["ticket_id"]),
                                 ("ignore", 0, kept, None))
        self.assertEqual(self.s.tickets(), [])

    def test_an_unknown_name_the_second_time_is_unavailable_and_opens_nothing(self):
        tid, _ = self.first_look()
        rep = self.finish(self.batch([tid]), {tid: ("no-such-rule", "x")})
        self.assertEqual(rep["unavailable"], [tid])
        self.assertEqual(self.s.tickets(), [])

    def test_observe_previews_only_what_two_looks_agree_on(self):
        agree, _ = self.first_look(ext="a", policy=OBSERVE)
        differ, _ = self.first_look(ext="b", policy=OBSERVE)
        self.finish(self.batch([agree]), {agree: ("export-bug", "w")}, OBSERVE)
        self.finish(self.batch([differ]), {differ: (None, "w")}, OBSERVE)
        d = self.decisions()
        self.assertEqual((d[agree]["would_accept"], d[differ]["would_accept"]), (1, 0))
        self.assertEqual(self.s.tickets(), [])

    def test_switching_the_second_look_off_between_the_looks_does_not_skip_it(self):
        """A candidate already waiting for its second look still gets it: dropping back to the first
        answer would open a ticket nobody confirmed, and asking the first look again would lose it."""
        tid, _ = self.first_look()
        off = policy("auto", confirm_inferred=False)
        rep = self.finish(self.batch([tid]), {tid: (None, "second words")}, off)
        self.assertEqual((rep["accepted"], self.s.tickets()), ([], []))
        self.assertEqual(self.decisions()[tid]["model_rule"], "")

    def test_an_answer_applied_twice_still_opens_one_ticket(self):
        tid, _ = self.first_look()
        bid = self.batch([tid])
        self.finish(bid, {tid: ("export-bug", "w")})
        with self.assertRaises(store_mod.StaleBatch):
            self.finish(bid, {tid: ("export-bug", "w")})
        self.assertEqual(len(self.s.tickets()), 1)

    def test_a_second_look_that_gets_no_usable_answer_ends_unavailable_and_says_which_rule_it_was_about(self):
        """Three misses is the limit for the second look too, counted from zero; the reason keeps the
        confirmation visible, so a reader sees a candidate that was only half-judged."""
        tid, _ = self.first_look()
        for k in range(EVAL_MAX_ATTEMPTS):
            self.assertIsNone(self.decisions().get(tid), f"decided too early, after {k} misses")
            self.s.requeue_eval(self.batch([tid]), "no reply")
        d = self.decisions()[tid]
        self.assertEqual(d["verdict"], "unavailable")
        self.assertIn("confirmation of export-bug", d["reason"])
        self.assertEqual(self.s.tickets(), [])


class TestAnOlderHome(unittest.TestCase):
    def test_a_home_made_before_the_second_look_is_brought_up_to_date(self):
        """`CREATE TABLE IF NOT EXISTS` adds no column to an existing table; a candidate that was waiting
        or in flight at the upgrade must still be a first look."""
        with tempfile.TemporaryDirectory() as home:
            s = Store(home).open()
            s.close()
            db = sqlite3.connect(os.path.join(home, "state.sqlite"))
            db.executescript("""
                ALTER TABLE eval_item RENAME TO eval_item_new;
                CREATE TABLE eval_item(trigger_id INTEGER PRIMARY KEY REFERENCES trigger(id), state TEXT NOT NULL,
                  batch_id INTEGER REFERENCES eval_batch(id), attempts INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL);
                DROP TABLE eval_item_new;
                INSERT INTO trigger(id, source, ext_id, thread, sender, text, addressed, ts)
                  VALUES(1, 's', 'e', 't', 'bob', 'x', 0, 1.0);
                INSERT INTO eval_item(trigger_id, state, attempts, ts) VALUES(1, 'pending', 1, 1.0);""")
            db.commit()
            db.close()
            s = Store(home).open()
            try:
                it = s.eval_item(1)
                self.assertEqual((it["phase"], it["first_rule"], it["attempts"]), ("first", None, 1))
                self.assertEqual([i["phase"] for i in s.peek_eval_items(5)], ["first"])
            finally:
                s.close()


class TestTheEngine(Rig):
    FIRST, SECOND = "FIRST-LOOK-WORDS-31", "SECOND-LOOK-WORDS-77"

    def reply(self, rule, why, tid=1):
        return {"text": json.dumps({"decisions": [{"trigger_id": tid, "rule": rule, "reason": why}]})}

    def test_the_second_look_is_asked_without_the_first_answer_and_in_a_new_session(self):
        """A session that kept several batches has seen the first answer; the second look would then be
        agreement with itself. With `reset_every=3` the engine must drop the session on its own."""
        self.make_engine(reset_every=3)
        self.llm.script[:] = [self.reply("export-bug", self.FIRST), self.reply("export-bug", self.SECOND)]
        self.drop("001.json", "the export fails")
        self.until(self.settled(1), what="a decision after two looks")
        self.assertEqual(len(self.llm.requests), 2)
        self.assertNotIn(self.FIRST, json.dumps(self.llm.requests[1]), "the first answer reached the second look")
        gens = sorted(n for n in os.listdir(os.path.join(self.home, "main")) if n.startswith("gen"))
        self.assertEqual(len(gens), 2, "the second look shared the session of the first")
        self.assertEqual(self.decisions()[1]["verdict"], "accept")
        self.assertEqual(len(self.engine.store.tickets()), 1)

    def test_a_restart_between_the_looks_still_ends_with_exactly_one_ticket(self):
        """The orchestrator dies while the second look is in flight. The candidate must still be waiting for its
        second look afterwards (not forgotten, not opened on the strength of the first answer), and it is
        asked again without a try being used up."""
        self.llm.script[:] = [self.reply("export-bug", self.FIRST), dict(self.reply("export-bug", self.SECOND), delay=30)]
        self.drop("001.json", "the export fails")
        self.until(lambda: self.engine.main.busy and len(self.llm.requests) >= 2
                   and (self.engine.store.eval_item(1) or {}).get("phase") == "confirm",
                   what="the second look to be in flight (its request has reached the endpoint)")
        self.assertEqual((self.engine.store.tickets(), self.engine.store.decisions()), ([], []))
        self.stores[-1].close()                                       # the orchestrator "crashes"; the child lives on
        self.engine.main.on_stop = self.engine.main.on_start = None
        self.llm.script[:] = [self.reply("export-bug", self.SECOND)]
        e2 = self.make_engine()
        e2.recover()
        it = e2.store.eval_item(1)
        self.assertEqual((it["state"], it["phase"], it["attempts"], it["first_rule"]), ("pending", "confirm", 0, "export-bug"))
        self.until(self.settled(1), what="the second look after the restart")
        self.assertEqual(len(self.engine.store.tickets()), 1, self.decisions())
        self.assertEqual(self.decisions()[1]["verdict"], "accept")

    def test_a_second_look_with_unreadable_replies_ends_unavailable_without_a_ticket(self):
        """The parser and the retry wiring are shared with the first look, but a break that only shows in the
        second one would otherwise open nothing and say nothing: the candidate must end as unavailable and say
        which rule was being confirmed."""
        self.llm.script[:] = [self.reply("export-bug", self.FIRST), {"text": "no"}, {"text": "still no"}, {"text": "never"}]
        self.drop("001.json", "the export fails")
        self.until(self.settled(1), what="the second look to give up")
        d = self.decisions()[1]
        self.assertEqual(d["verdict"], "unavailable")
        self.assertIn("confirmation of export-bug", d["reason"])
        self.assertEqual(self.engine.store.tickets(), [])
        self.assertEqual(len(self.llm.requests), 4)

    def test_a_second_look_that_takes_too_long_is_asked_again_and_opens_nothing_meanwhile(self):
        """A late answer to a second look must not open a ticket either (the deadline is checked first), and
        the batch is asked again with the first look still on record."""
        self.llm.script[:] = [self.reply("export-bug", self.FIRST), dict(self.reply("export-bug", self.SECOND), delay=20),
                              self.reply("export-bug", self.SECOND)]
        self.drop("001.json", "the export fails")
        self.until(lambda: self.engine.main.busy and len(self.llm.requests) >= 2
                   and (self.engine.store.eval_item(1) or {}).get("phase") == "confirm", what="the second look in flight")
        self.now[0] += 1000                                           # the clock moves on; the model does not answer
        self.until(self.settled(1), what="the second try of the second look")
        self.assertEqual(self.engine.store.eval_item(1)["attempts"], 1)
        self.assertTrue([e for e in self.engine.events if e["kind"] == "eval_timeout"])
        self.assertEqual(self.decisions()[1]["verdict"], "accept")
        self.assertEqual(len(self.engine.store.tickets()), 1, "exactly one ticket, from the answer that arrived in time")

    def test_two_different_answers_leave_a_decision_that_shows_the_disagreement(self):
        self.llm.script[:] = [self.reply("export-bug", self.FIRST), self.reply(None, self.SECOND)]
        self.drop("001.json", "maybe the export fails")
        self.until(self.settled(1))
        d = self.decisions()[1]
        self.assertEqual((d["verdict"], d["model_rule"]), ("ignore", ""))
        self.assertIn("second look differs", d["reason"])
        self.assertEqual(self.engine.store.tickets(), [])


class TestTheReplay(ReplayCase):
    def test_a_rule_the_second_look_does_not_repeat_is_not_counted_as_a_hit(self):
        """The measurement must see what a live run does: a disagreement is a miss for a positive row and
        a correct 'no' for a negative one, and the report says how many looks disagreed."""
        def table(text, n):
            if n == 1:
                return {"text 1": "a", "text 2": "a", "text 3": NULL}[text]
            return {"text 1": "a", "text 2": NULL}[text]              # the second look: row 1 repeats, row 2 does not
        self.script(table)
        rep = rp.replay(self.corpus(row(1, "a"), row(2, None), row(3, None)), self.cfg(confirm=True, batch_max=3))
        self.assertEqual([r["outcomes"][0] for r in rep["rows"]], ["a", NULL, NULL])
        self.assertEqual([r["first"] for r in rep["rows"]], ["a", "a", None])
        self.assertEqual(rep["confirmation"], {"asked": 2, "confirmed": 1, "disagreed": 1, "unavailable": 0})
        self.assertEqual((rep["primary"]["recall"]["k"], rep["primary"]["specificity"]["k"]), (1, 2))
        self.assertTrue(rep["meta"]["confirm"])

    def test_a_chunk_that_runs_out_of_time_during_a_second_look_is_still_counted_as_asked(self):
        """The row's first look is on record in the ledger even though no decision exists yet; leaving it out
        would make the confirmation numbers claim fewer second looks than were due."""
        def table(text, n):
            return "a"
        s = self.script(table)
        orig = s.pop

        def slow_second(_):
            out = orig(_)
            if s.calls >= 2:
                out["delay"] = 10                      # the second look is slower than the chunk's time
            return out
        s.pop = slow_second
        rep = rp.replay(self.corpus(row(1, "a")), self.cfg(confirm=True, chunk_timeout=4.0))
        self.assertEqual(rep["rows"][0]["outcomes"], [UNAVAILABLE])
        self.assertEqual(rep["rows"][0]["first"], "a")
        self.assertEqual(rep["confirmation"], {"asked": 1, "confirmed": 0, "disagreed": 0, "unavailable": 1})

    def test_two_different_rules_are_not_a_hit_for_either(self):
        def table(text, n):
            return "a" if n == 1 else "b"
        self.script(table)
        rep = rp.replay(self.corpus(row(1, "a")), self.cfg(confirm=True))
        self.assertEqual(rep["rows"][0]["outcomes"], [NULL])
        self.assertEqual(rep["confirmation"]["disagreed"], 1)

    def test_an_unknown_name_the_second_time_is_unavailable_in_the_count(self):
        def table(text, n):
            return "a" if n == 1 else "zzz"
        self.script(table)
        rep = rp.replay(self.corpus(row(1, "a")), self.cfg(confirm=True))
        self.assertEqual(rep["rows"][0]["outcomes"], [UNAVAILABLE])
        self.assertEqual(rep["confirmation"], {"asked": 1, "confirmed": 0, "disagreed": 0, "unavailable": 1})

    def test_without_confirmation_the_classification_is_what_it_was_and_the_report_says_so(self):
        """`--no-confirm` must reproduce the old single-look measurement; only the labels about the run change."""
        self.script(lambda text, n: "a")
        rep = rp.replay(self.corpus(row(1, "a"), row(2, None)), self.cfg(confirm=False))
        self.assertEqual([r["outcomes"] for r in rep["rows"]], [["a"], ["a"]])
        self.assertEqual((rep["confirmation"], rep["meta"]["confirm"], [r["first"] for r in rep["rows"]]),
                         (None, False, [None, None]))
        self.assertEqual(self.llm.requests.__len__(), 1, "one look only")

    def test_the_outcome_of_a_decision_with_and_without_confirmation(self):
        """The same stored decision means different things: an ignore that remembers the rule the model
        named last was a hit under the old reading and is not one now."""
        d = {"verdict": "ignore", "model_rule": "a", "reason": "second look differs", "would_accept": 0}
        self.assertEqual(rp.outcome_of(d, {"a"}), ("a", None))
        self.assertEqual(rp.outcome_of(d, {"a"}, confirm=True), (NULL, None))
        stood = dict(d, would_accept=1)
        self.assertEqual(rp.outcome_of(stood, {"a"}, confirm=True), ("a", None))


if __name__ == "__main__":
    unittest.main()
