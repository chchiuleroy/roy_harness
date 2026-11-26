"""verify.py: the one-command entry that runs the harness suite and the kb suite.

Each test says why the behaviour matters: the entry exists so that "verified" can never mean "half was run"."""
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import verify  # noqa: E402


def stage(name, code, needs=()):
    """A fake stage: a child Python process running `code` (nothing here touches the real suites)."""
    return {"name": name, "cwd": ROOT, "needs": needs, "cmd": [sys.executable, "-c", code]}


def run(argv, stages, find=None, environ=None):
    out = io.StringIO()
    kw = {"environ": environ} if environ is not None else {}
    if find is not None:
        kw["find"] = find
    code = verify.main(argv, stages=stages, out=out, **kw)
    return code, out.getvalue()


class VerifyEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def marker(self, name):
        return os.path.join(self.tmp.name, name).replace(os.sep, "/")

    def writes_marker(self, name):
        return f"open({self.marker(name)!r}, 'w').close()"

    def test_all_stages_passing_gives_exit_0_and_the_test_counts(self):
        # unittest prints "Ran N tests in Xs" on stderr; the summary must carry N so a run that found 0 tests is visible
        ok = "import sys; sys.stderr.write('Ran 3 tests in 0.1s\\n')"
        code, text = run([], [stage("harness", ok), stage("kb", ok)])
        self.assertEqual(code, 0)
        self.assertIn("OK   harness  (3 tests)", text)
        self.assertIn("verify: PASS", text)

    def test_a_failing_first_stage_does_not_hide_the_second(self):
        # why: after a harness failure, "kb still green?" must still be answered in the same run
        code, text = run([], [stage("harness", "raise SystemExit(1)"), stage("kb", self.writes_marker("kb_ran"))])
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(self.marker("kb_ran")), "second stage was skipped after the first failed")
        self.assertIn("FAIL harness", text)
        self.assertIn("OK   kb", text)

    def test_a_stage_that_cannot_run_is_a_failure_not_a_skip(self):
        # why: kb without its optional packages would otherwise let "verify passed" mean "kb was never run"
        code, text = run([], [stage("kb", self.writes_marker("kb_ran"), needs=("numpy",))], find=lambda m: None)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.marker("kb_ran")), "the stage ran although its needs were missing")
        self.assertIn("missing optional package(s) numpy", text)
        self.assertIn("verify: FAIL", text)

    def test_only_runs_just_the_selected_stage(self):
        code, _ = run(["--only", "kb"], [stage("harness", self.writes_marker("h")), stage("kb", self.writes_marker("k"))])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.marker("k")))
        self.assertFalse(os.path.exists(self.marker("h")), "--only kb still ran the harness stage")

    def test_selecting_no_stage_is_not_a_pass(self):
        # why: an empty run must not look green (for example a stage renamed while the caller still says --only kb)
        code, text = run(["--only", "kb"], [stage("harness", "pass")])
        self.assertEqual(code, 1)
        self.assertIn("verify: FAIL", text)

    def test_live_switches_never_reach_the_child_process(self):
        # why: this is the offline check; a stray HARNESS_LIVE in the shell must not turn it into a run against a real server
        probe = ("import os; print('LIVE=' + repr([k for k in ('HARNESS_LIVE', 'SUPERAGENT_LIVE', 'SUPERAGENT_REMOVAL') "
                 "if k in os.environ]), 'KEEP=' + os.environ.get('VERIFY_KEEP', ''))")
        env = dict(os.environ, HARNESS_LIVE="1", SUPERAGENT_LIVE="1", SUPERAGENT_REMOVAL="1", VERIFY_KEEP="yes")
        code, text = run([], [stage("harness", probe)], environ=env)
        self.assertEqual(code, 0)
        self.assertIn("LIVE=[] KEEP=yes", text)  # live switches gone, ordinary variables kept
        self.assertIn("removed HARNESS_LIVE, SUPERAGENT_LIVE, SUPERAGENT_REMOVAL", text)

    def test_default_stages_point_at_files_that_exist(self):
        # why: the real stage commands are never run by these tests, so a moved folder would only show up at run time
        by_name = {s["name"]: s for s in verify.default_stages()}
        self.assertEqual(set(by_name), {"harness", "kb"})
        self.assertTrue(os.path.isdir(os.path.join(by_name["harness"]["cwd"], "tests")))
        self.assertTrue(os.path.isfile(os.path.join(by_name["kb"]["cwd"], "run_tests.py")))
        self.assertEqual(by_name["harness"]["needs"], ())
        self.assertEqual(set(by_name["kb"]["needs"]), {"numpy", "fitz"})


if __name__ == "__main__":
    unittest.main()
