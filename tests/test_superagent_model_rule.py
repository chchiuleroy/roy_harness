"""Slice 4, S1: what the model said is kept on the decision (`model_rule`), and the text the model wrote
is cleaned before it is stored or shown.

Each test's docstring says WHY the behaviour matters.
"""
import json
import os
import re
import sqlite3
import tempfile
import unittest

import superagent_support as sup  # noqa: F401
from superagent import sanitize
from superagent import store as store_mod
from superagent.gate import UNKNOWN_RULE_MAX, Policy, Rule, authorize
from superagent.model import Decision, Strength, Verdict
from superagent.store import EVAL_MAX_ATTEMPTS, Store
from test_superagent_eval_store import AUTO, OBSERVE, Ledger, accept, ignore, unavailable

OFF = Policy(allowed_senders=frozenset({"alice"}), accept_mode="off", rules=AUTO.rules)


class TestTheWordNullMeansNoRule(unittest.TestCase):
    """Found on 2026-11-06 by re-running the 41 rows that had come back unavailable: all 27 answers were the
    STRING "null" (the batch template shows `"<rule name>" or null`, and the model sometimes quotes the
    second half too). It meant 'no rule'; read as an unknown name it was recorded as unavailable and counted
    against the model. The move is only ever toward 'do nothing', so it cannot open a ticket."""

    def test_the_string_null_is_the_same_as_no_rule_in_every_mode(self):
        for word in ("null", "NULL", " Null" + chr(10)):          # chr(10): a newline after the word
            for policy in (AUTO, OBSERVE, OFF):
                with self.subTest(word=word, mode=policy.accept_mode):
                    d = authorize(word, "chat", Strength.INFERRED, policy).decision
                    same = authorize(None, "chat", Strength.INFERRED, policy).decision
                    self.assertEqual((d.verdict, d.would_accept, d.model_rule, d.reason),
                                     (same.verdict, same.would_accept, same.model_rule, same.reason))
                    self.assertEqual((d.verdict, d.model_rule), (Verdict.IGNORE, ""))

    def test_other_words_are_still_unknown_names(self):
        """Only the one word the evidence shows is mapped; 'none', 'n/a' and the like stay unavailable, so
        nothing else the model might write is quietly treated as an answer."""
        for word in ("none", "n/a", "nul", "no-rule", ""):
            with self.subTest(word=word):
                self.assertEqual(authorize(word, "x", Strength.INFERRED, AUTO).decision.verdict, Verdict.UNAVAILABLE)

    def test_a_rule_that_is_really_named_null_still_works(self):
        """Rule names may be any of a-z, 0-9 and '-', so 'null' is a legal name; mapping it away would make
        that rule unusable."""
        pol = Policy(allowed_senders=frozenset({"alice"}), accept_mode="auto", rules=(Rule("null", "weird name"),))
        d = authorize("null", "x", Strength.INFERRED, pol).decision
        self.assertEqual((d.verdict, d.model_rule), (Verdict.ACCEPT, "null"))

    def test_a_second_look_that_says_null_is_a_disagreement_not_a_failure(self):
        """It is the model changing its mind, not an answer nobody could use: the candidate is ignored with
        the disagreement on record, and the unavailable count is not inflated."""
        from superagent.gate import confirm
        d = confirm("export-bug", "one", "null", "two", AUTO).decision
        self.assertEqual((d.verdict, d.model_rule), (Verdict.IGNORE, ""))
        self.assertIn("second look differs", d.reason)


class TestTheWordNullInTheLedger(Ledger):
    def test_it_is_recorded_as_done_with_no_rule_and_opens_nothing(self):
        """End to end through the store, which is the only place an answer becomes a decision."""
        tid = self.candidate("a")
        rep = self.finish(self.batch([tid]), {tid: ("null", "no")})
        self.assertEqual((rep["done"], rep["unavailable"], rep["accepted"]), ([tid], [], []))
        self.assertEqual(self.decisions()[tid]["model_rule"], "")
        self.assertEqual(self.s.tickets(), [])


class TestAuthorizeKeepsWhatTheModelSaid(unittest.TestCase):
    def test_no_rule_is_an_empty_string_not_none(self):
        """None means 'nobody answered'; '' means 'the model answered: no rule'. A measurement that
        mixed the two would count a declined row as a correct 'no'."""
        for policy in (AUTO, OBSERVE, OFF):
            with self.subTest(mode=policy.accept_mode):
                self.assertEqual(authorize(None, "chat", Strength.INFERRED, policy).decision.model_rule, "")

    def test_a_known_rule_is_kept_in_every_mode(self):
        """observe used to keep the rule name only inside the reason text."""
        for policy in (AUTO, OBSERVE, OFF):
            with self.subTest(mode=policy.accept_mode):
                self.assertEqual(authorize("export-bug", "x", Strength.INFERRED, policy).decision.model_rule,
                                 "export-bug")

    def test_an_unknown_rule_is_kept_cleaned_and_cut(self):
        """It is the model's own text, so it goes into the audit trail only after it is made harmless."""
        evil = "bill‮ing\u009b\x1b[31m" + "x" * 200 + "\ud800\n"
        d = authorize(evil, "x", Strength.INFERRED, AUTO).decision
        self.assertEqual(d.verdict, Verdict.UNAVAILABLE)
        self.assertLessEqual(len(d.model_rule), UNKNOWN_RULE_MAX)
        self.assertTrue(d.model_rule.startswith("billing[31mx"), d.model_rule)   # the escape byte goes, its letters stay
        self.assertFalse(any(sanitize.is_unsafe(ch) for ch in d.model_rule))

    def test_the_cleaning_happens_before_the_cut(self):
        """If it were the other way round, 63 invisible characters would leave an empty name."""
        d = authorize("‮" * 100 + "billing", "x", Strength.INFERRED, AUTO).decision
        self.assertEqual(d.model_rule, "billing")

    def test_a_name_that_is_not_text_is_kept_as_text(self):
        d = authorize(5, "x", Strength.INFERRED, AUTO).decision
        self.assertEqual((d.verdict, d.model_rule), (Verdict.UNAVAILABLE, "5"))

    def test_a_lone_surrogate_in_the_models_reason_cannot_crash_the_store(self):
        """Found while writing the view tests: a JSON "\\ud800" escape gives the model a way to hand over a
        string that cannot be stored as UTF-8. record_decision raised UnicodeEncodeError and the
        orchestrator went down with it. The reason is cleaned before it is kept."""
        d = authorize("export-bug", "bad \ud800 reason\n‮", Strength.INFERRED, OBSERVE).decision
        self.assertEqual(d.reason, "observe: would accept (rule export-bug): bad  reason")
        with tempfile.TemporaryDirectory() as home, Store(home) as s:
            t = s.add_trigger(__import__("test_superagent_eval_store").trig("x"))
            s.record_decision(t, d)                                  # used to raise
            self.assertEqual(s.decisions()[0]["reason"], d.reason)

    def test_the_decision_itself_is_unchanged(self):
        """model_rule is a record, not a ground: the verdicts are what they were."""
        self.assertEqual(authorize("export-bug", "x", Strength.INFERRED, AUTO).decision.verdict, Verdict.ACCEPT)
        d = authorize("export-bug", "x", Strength.INFERRED, OBSERVE).decision
        self.assertEqual((d.verdict, d.would_accept), (Verdict.IGNORE, True))
        self.assertEqual(authorize(None, "x", Strength.INFERRED, AUTO).decision.verdict, Verdict.IGNORE)

    def test_older_code_that_builds_a_decision_without_it_still_works(self):
        d = Decision(Verdict.IGNORE, Strength.INFERRED, "why", True)
        self.assertIsNone(d.model_rule)


class TestStoredWithTheDecision(Ledger):
    def test_each_kind_of_answer_is_stored_as_given(self):
        a, b, c = self.candidate("a"), self.candidate("b"), self.candidate("c")
        bid = self.batch([a, b, c])
        self.finish(bid, {a: accept(), b: ignore(), c: unavailable()})
        got = {t: d["model_rule"] for t, d in self.decisions().items()}
        self.assertEqual(got, {a: "export-bug", b: "", c: "no-such-rule"})

    def test_a_candidate_given_up_on_has_no_model_answer(self):
        """After the tries run out nothing was ever answered, so it is NULL: 'unknown rule' and 'gave up'
        stay distinguishable."""
        a = self.candidate("a")
        for _ in range(EVAL_MAX_ATTEMPTS):
            bid = self.batch([a])
            self.s.requeue_eval(bid, "unreadable reply")
        d = self.decisions()[a]
        self.assertEqual((d["verdict"], d["model_rule"]), ("unavailable", None))

    def test_a_rule_based_decision_has_no_model_answer(self):
        t = self.s.add_trigger(__import__("test_superagent_eval_store").trig("x"))
        self.s.record_decision(t, Decision(Verdict.IGNORE, Strength.NOT_ADMITTED, "not admitted"))
        self.assertIsNone(self.s.decisions()[0]["model_rule"])


def old_schema():
    """The schema as it was before the column existed."""
    old = re.sub(r",\n  model_rule TEXT\);[^\n]*", ");", store_mod.SCHEMA)
    assert "model_rule" not in old
    return old


class TestOlderDatabase(unittest.TestCase):
    def make_old_home(self, d):
        db = sqlite3.connect(os.path.join(d, "state.sqlite"))
        db.executescript(old_schema())
        db.execute("INSERT INTO trigger(id, source, ext_id, thread, sender, text, addressed, ts) "
                   "VALUES(1,'s','e1','t','bob','hello',0,1.0)")
        db.execute("INSERT INTO decision(trigger_id, verdict, strength, reason, would_accept, ts) "
                   "VALUES(1,'ignore','inferred','observe: would accept (rule x): y',1,1.0)")
        db.commit()
        db.close()

    def test_an_older_home_gets_the_column_and_old_rows_stay_null(self):
        """Found by the same mistake as before (a column added only to CREATE TABLE IF NOT EXISTS). And
        the old rows must stay NULL: filling in '' would turn 'not recorded' into 'the model said
        no rule', and the measurement would count it."""
        with tempfile.TemporaryDirectory() as d:
            self.make_old_home(d)
            with Store(d) as s:
                self.assertIn("model_rule", {r["name"] for r in s._q("PRAGMA table_info(decision)")})
                self.assertIsNone(s.decisions()[0]["model_rule"])
                t = s.add_trigger(__import__("test_superagent_eval_store").trig("n"))
                s.record_decision(t, Decision(Verdict.IGNORE, Strength.INFERRED, "x", model_rule="r"))
                self.assertEqual([x["model_rule"] for x in s.decisions()], [None, "r"])
            with Store(d) as s:                                 # and a second open changes nothing
                self.assertEqual([x["model_rule"] for x in s.decisions()], [None, "r"])


class TestSanitize(unittest.TestCase):
    TRICKS = "a‮b\u009bc​d⁦e﻿­f g h\x00i\x7fj\U000E0041k\ud800l"

    def test_clean_removes_every_unsafe_character_but_keeps_layout(self):
        self.assertEqual(sanitize.clean(self.TRICKS), "abcdefghijkl")
        self.assertEqual(sanitize.clean("line 1\n\tline 2"), "line 1\n\tline 2")

    def test_clean_line_also_drops_newline_and_tab(self):
        self.assertEqual(sanitize.clean_line("a\nb\tc"), "abc")

    def test_real_text_is_left_alone(self):
        text = "匯出失敗 café (é) \U0001F468‍\U0001F469 مرحبا"
        self.assertEqual(sanitize.clean(text), text)
        self.assertEqual(sanitize.visible(text), text)

    def test_visible_spells_what_it_removes_including_layout(self):
        out = sanitize.visible("a‮b\nc\td\U000E0041")
        self.assertEqual(out, "a\\u202eb\\u000ac\\u0009d\\udb40\\udc41")
        self.assertTrue(out.isascii())

    def test_json_survives_the_round_trip_and_hides_nothing(self):
        """The export keeps Chinese readable (ensure_ascii=False) but spells every unsafe character, and
        parsing it gives back exactly what was written."""
        for original in (self.TRICKS, "plain", "匯出‮失敗\n", "\ud83d"):
            with self.subTest(original=original[:12]):
                dumped = sanitize.json_safe(json.dumps({"text": original}, ensure_ascii=False))
                self.assertFalse(any(sanitize.is_unsafe(ch) and ch not in "\n\t" for ch in dumped))
                self.assertEqual(json.loads(dumped)["text"], original)

    def test_reply_cleaning_and_the_stored_name_use_the_same_definition(self):
        """One definition, so the two cannot drift apart."""
        from superagent.turnreply import clean_reply
        self.assertEqual(clean_reply(self.TRICKS)[0], sanitize.clean(self.TRICKS))


if __name__ == "__main__":
    unittest.main()
