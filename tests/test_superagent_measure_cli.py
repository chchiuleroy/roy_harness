"""Slice 4: the two commands on the command line (decisions, eval) -- argument handling and what reaches
stdout / a file. The numbers themselves are tested in the replay and measure tests.

Each test's docstring says WHY the behaviour matters.
"""
import contextlib
import io
import json
import os
import re
import tempfile
import unittest

import superagent_support as sup
from superagent.__main__ import main
from superagent.model import Decision, Strength, Trigger, Verdict
from superagent.store import Store

RULES = ["a=the first rule", "b=the second rule"]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class Cli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.home = os.path.join(self.dir, "home")

    def tearDown(self):
        self._tmp.cleanup()

    def corpus(self, rows):
        path = os.path.join(self.dir, "c.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return path


class TestDecisionsCommand(Cli):
    def populate(self):
        with Store(self.home) as s:
            with s.tx():
                for i in (1, 2):
                    s.add_trigger(Trigger("file:inbox", f"e{i}", f"t{i}", "ann", f"text {i}", False, 1.0))
                    s.enqueue_eval(i)
            s.record_decision(1, Decision(Verdict.IGNORE, Strength.INFERRED, "SECRET-REASON", True, model_rule="a"))
            s.record_decision(2, Decision(Verdict.IGNORE, Strength.INFERRED, "r", model_rule=""))

    def test_table_jsonl_and_blind_export(self):
        self.populate()
        code, out, _ = run(["--home", self.home, "decisions"])
        self.assertEqual(code, 0)
        self.assertIn("text 1", out)
        code, out, _ = run(["--home", self.home, "decisions", "--jsonl", "--would-accept"])
        self.assertEqual([json.loads(x)["decision_id"] for x in out.splitlines()], [1])
        code, out, _ = run(["--home", self.home, "decisions", "--for-labeling"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertNotIn('"label"', out)
        self.assertNotIn('"decision_id"', out)
        for bad in (["--card-cap", "0"], ["--card-cap", "-3"]):
            code, o, err = run(["--home", self.home, "decisions", "--for-labeling", *bad])
            self.assertEqual((code, o), (1, ""))
            self.assertIn("card_cap", err)
        self.assertNotIn("SECRET-REASON", out)
        self.assertNotIn('"verdict"', out)

    def test_a_home_that_never_ran_exits_1_with_a_message(self):
        code, out, err = run(["--home", os.path.join(self.dir, "none"), "decisions"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("never been run", err)

    def test_a_bad_since_or_verdict_is_a_message_not_a_traceback(self):
        self.populate()
        for extra in (["--since", "yesterday"], ["--verdict", "approved"], ["--limit", "0"]):
            with self.subTest(extra=extra):
                code, out, err = run(["--home", self.home, "decisions", *extra])
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("[superagent]"), err)

    def test_it_does_not_create_the_home(self):
        run(["--home", self.home, "decisions"])
        self.assertFalse(os.path.exists(self.home))


class TestEvalCommand(Cli):
    def setUp(self):
        super().setUp()
        self.llm = sup.FakeLLM([])

    def tearDown(self):
        self.llm.close()
        super().tearDown()

    def argv(self, path, *extra):
        rules = [x for r in RULES for x in ("--rule", r)]
        return ["eval", "--corpus", path, *rules, "--base-url", self.llm.base_url, "--model", "fake-model",
                "--backoff", "0.01", *extra]

    def test_bad_input_is_refused_with_every_problem_listed(self):
        path = self.corpus([{"id": "x", "text": "", "label": "zzz"}, {"id": "x", "text": "t", "label": None}])
        code, out, err = run(self.argv(path))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("eval refused", err)
        self.assertGreaterEqual(len(err.strip().splitlines()), 3, "all problems, not only the first")

    def test_bad_settings_are_refused(self):
        path = self.corpus([{"id": "x", "text": "t", "label": None}])
        for extra in (["--repeats", "0"], ["--repeats", "21"], ["--half-width", "0"], ["--confidence", "1"]):
            with self.subTest(extra=extra):
                code, out, err = run(self.argv(path, *extra))
                self.assertEqual((code, out), (1, ""))
                self.assertIn("eval refused", err)

    def test_a_missing_endpoint_or_rule_is_a_usage_error(self):
        path = self.corpus([{"id": "x", "text": "t", "label": None}])
        self.assertEqual(run(["eval", "--corpus", path, "--rule", "a=x"])[0], 2)
        self.assertEqual(run(["eval", "--corpus", path, "--base-url", "http://localhost:1/v1", "--model", "m"])[0], 2)

    def test_a_run_writes_a_report_that_says_what_it_is(self):
        self.llm.script = [{"text": json.dumps({"decisions": [
            {"trigger_id": 1, "rule": "a", "reason": "SECRET-REASON"},
            {"trigger_id": 2, "rule": None, "reason": "SECRET-REASON"}]})}]
        path = self.corpus([{"id": "r1", "text": "PRIVATE-WORDS about a", "label": "a"},
                            {"id": "r2", "text": "PRIVATE-WORDS about nothing", "label": None}])
        report_path = os.path.join(self.dir, "report.json")
        scratch = os.path.join(self.dir, "tmp")             # a private temp root: other processes cannot add to it
        os.makedirs(scratch)
        saved, tempfile.tempdir = tempfile.tempdir, scratch
        try:
            code, out, err = run(self.argv(path, "--out", report_path))
        finally:
            tempfile.tempdir = saved
        self.assertEqual(code, 0, err)
        self.assertIn(report_path, out)
        with open(report_path, encoding="utf-8") as f:
            text = f.read()
        rep = json.loads(text)
        self.assertEqual((rep["primary"]["recall"]["k"], rep["primary"]["specificity"]["k"]), (1, 1))
        for leak in ("PRIVATE-WORDS", "SECRET-REASON", self.llm.base_url):
            self.assertNotIn(leak, text)
        self.assertNotRegex(text, r"(?i)\bpassed\b|\bfailed\b|\bgood enough\b(?!\.)|\bacceptable\b")
        self.assertEqual(os.listdir(scratch), [], "the replay's temporary folders must be gone")


if __name__ == "__main__":
    unittest.main()
