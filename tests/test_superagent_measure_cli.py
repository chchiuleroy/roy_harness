"""Slice 4: the two commands on the command line (decisions, eval) -- argument handling and what reaches
stdout / a file. The numbers themselves are tested in the replay and measure tests.

Each test's docstring says WHY the behaviour matters.
"""
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import superagent_support as sup
from superagent.__main__ import main
from superagent.engine import Engine
from superagent.main_session import MainSession
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
            {"trigger_id": 2, "rule": None, "reason": "SECRET-REASON"}]})},
            {"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": "a", "reason": "SECRET-REASON"}]})}]
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


class TestOperatingCommands(Cli):
    """The commands a person uses by hand: label (make the page), eval with progress and a readable
    summary, report (read a saved report)."""

    def setUp(self):
        super().setUp()
        self.llm = sup.FakeLLM([])

    def tearDown(self):
        self.llm.close()
        super().tearDown()

    def messages(self, n=2):
        path = os.path.join(self.dir, "msgs.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(f"message {i}" for i in range(1, n + 1)) + "\n")
        return path

    def test_label_writes_a_page_and_says_what_to_do_next(self):
        out_path = os.path.join(self.dir, "label.html")
        code, out, err = run(["label", "--input", self.messages(3), "--out", out_path,
                              "--rule", RULES[0], "--rule", RULES[1]])
        self.assertEqual(code, 0, err)
        self.assertIn("3 messages", out)
        with open(out_path, encoding="utf-8") as f:
            html = f.read()
        self.assertIn('"name": "a"', html)
        self.assertIn("message 2", html)

    def test_label_refuses_what_it_cannot_use_with_a_message(self):
        out_path = os.path.join(self.dir, "label.html")
        code, out, err = run(["label", "--input", os.path.join(self.dir, "missing.txt"), "--out", out_path, "--rule", RULES[0]])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("label refused", err)
        empty = os.path.join(self.dir, "empty.txt")
        open(empty, "w").close()
        code, _, err = run(["label", "--input", empty, "--out", out_path, "--rule", RULES[0]])
        self.assertEqual(code, 1)
        code, _, err = run(["label", "--input", self.messages(), "--out", out_path, "--rule", RULES[0], "--card-cap", "0"])
        self.assertEqual(code, 1)
        self.assertEqual(run(["label", "--input", self.messages(), "--out", out_path])[0], 2, "no rule is a usage error")
        self.assertFalse(os.path.exists(out_path), "nothing is written when it refuses")

    def test_eval_prints_progress_and_a_summary_and_quiet_turns_progress_off(self):
        self.llm.script = [{"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": "a", "reason": "r"},
                                                            {"trigger_id": 2, "rule": None, "reason": "r"}]})},
            {"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": "a", "reason": "r"}]})}]
        path = self.corpus([{"id": "r1", "text": "about a", "label": "a"}, {"id": "r2", "text": "about nothing", "label": None}])
        rules = [x for r in RULES for x in ("--rule", r)]
        base = ["eval", "--corpus", path, *rules, "--base-url", self.llm.base_url, "--model", "fake-model", "--backoff", "0.01"]
        report_path = os.path.join(self.dir, "r.json")
        code, out, err = run(base + ["--out", report_path, "--errors"])
        self.assertEqual(code, 0, err)
        self.assertIn("[eval] pass 1/1: batch 1/1", err)
        self.assertIn("召回 recall", out)
        self.assertIn("答錯或沒答案的計分列（第 1 次）：0", out)
        self.llm.script = [{"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": "a", "reason": "r"},
                                                            {"trigger_id": 2, "rule": None, "reason": "r"}]})},
            {"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": "a", "reason": "r"}]})}]
        code, out, err = run(base + ["--out", report_path, "--quiet"])
        self.assertEqual((code, "[eval]" in err), (0, False))
        # without --out the JSON itself goes to stdout and no summary is mixed into it
        self.llm.script = [{"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": "a", "reason": "r"},
                                                            {"trigger_id": 2, "rule": None, "reason": "r"}]})},
            {"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": "a", "reason": "r"}]})}]
        code, out, err = run(base + ["--quiet"])
        self.assertEqual(json.loads(out)["schema"], 1)

    def test_report_reads_a_saved_file_and_lists_errors_with_the_corpus_text(self):
        self.llm.script = [{"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": None, "reason": "r"},
                                                            {"trigger_id": 2, "rule": None, "reason": "r"}]})}]
        path = self.corpus([{"id": "r1", "text": "this one is about a", "label": "a"}, {"id": "r2", "text": "x", "label": None}])
        rules = [x for r in RULES for x in ("--rule", r)]
        report_path = os.path.join(self.dir, "r.json")
        run(["eval", "--corpus", path, *rules, "--base-url", self.llm.base_url, "--model", "fake-model",
             "--backoff", "0.01", "--out", report_path, "--quiet"])
        code, out, err = run(["report", report_path])
        self.assertEqual(code, 0, err)
        self.assertIn("召回 recall", out)
        self.assertNotIn("this one is about a", out)
        code, out, _ = run(["report", report_path, "--errors", "--corpus", path])
        self.assertIn("r1  標註 a  模型 null  | this one is about a", out)

    def test_an_output_path_that_cannot_be_written_is_a_message_not_a_traceback(self):
        missing = os.path.join(self.dir, "no_such_folder", "x")
        code, out, err = run(["label", "--input", self.messages(), "--out", missing + ".html", "--rule", RULES[0]])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("cannot write the page", err)
        self.llm.script = [{"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": "a", "reason": "r"}]})}]
        path = self.corpus([{"id": "r1", "text": "about a", "label": "a"}])
        rules = [x for r in RULES for x in ("--rule", r)]
        locked = os.path.join(self.dir, "locked.json")                 # the folder exists; the file cannot be written
        with open(locked, "w") as f:
            f.write("{}")
        os.chmod(locked, stat.S_IREAD)
        self.addCleanup(lambda: os.path.exists(locked) and os.chmod(locked, stat.S_IWRITE))
        code, out, err = run(["eval", "--corpus", path, *rules, "--base-url", self.llm.base_url, "--model", "fake-model",
                              "--backoff", "0.01", "--quiet", "--out", locked])
        self.assertEqual(code, 1)
        self.assertIn("cannot write the report", err)
        self.assertEqual(json.loads(out)["schema"], 1, "the finished report is not lost: it is on stdout")
        self.assertEqual(json.loads(out)["corpus"]["rows"], 1)

    def test_an_output_folder_that_does_not_exist_is_refused_before_any_model_work(self):
        """Operating trial (小o): the bad path was only found after the whole measurement had run."""
        path = self.corpus([{"id": "r1", "text": "about a", "label": "a"}])
        rules = [x for r in RULES for x in ("--rule", r)]
        for target in (os.path.join(self.dir, "nope", "r.json"), self.dir):        # a missing folder; a folder as the file
            with self.subTest(target=target):
                code, out, err = run(["eval", "--corpus", path, *rules, "--base-url", self.llm.base_url,
                                      "--model", "fake-model", "--out", target])
                self.assertEqual((code, out), (1, ""))
                self.assertIn("nothing was run", err)
        self.assertEqual(self.llm.requests, [], "the model was never called")

    def test_hashed_ids_still_get_their_message_openings_in_the_error_list(self):
        """Second review: --hash-ids changed the ids in the report, and the snippets were looked up by the
        original ids, so no opening was ever shown."""
        from superagent.corpus import hashed_id
        self.llm.script = [{"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": None, "reason": "r"}]})}]
        path = self.corpus([{"id": "r1", "text": "this one is about a", "label": "a"}])
        rules = [x for r in RULES for x in ("--rule", r)]
        report_path = os.path.join(self.dir, "r.json")
        code, out, err = run(["eval", "--corpus", path, *rules, "--base-url", self.llm.base_url, "--model", "fake-model",
                              "--backoff", "0.01", "--quiet", "--hash-ids", "--errors", "--out", report_path])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{hashed_id('r1')}  標註 a  模型 null  | this one is about a", out)
        self.assertNotIn("r1  標註", out)
        code, out, _ = run(["report", report_path, "--errors", "--corpus", path])
        self.assertIn(f"{hashed_id('r1')}  標註 a  模型 null  | this one is about a", out)

    def test_report_on_a_bad_file_is_a_message_not_a_traceback(self):
        junk = os.path.join(self.dir, "junk.json")
        with open(junk, "w") as f:
            f.write('{"not": "a report"}')
        for target in (junk, os.path.join(self.dir, "nope.json")):
            code, out, err = run(["report", target])
            self.assertEqual((code, out), (1, ""))
            self.assertIn("cannot read the report", err)


class TestOperatingFixes(Cli):
    """Found by 小o operating the tool for the first time (docs only + --help), each with its reproduction."""

    def setUp(self):
        super().setUp()
        self.llm = sup.FakeLLM([])

    def tearDown(self):
        self.llm.close()
        super().tearDown()

    def test_run_once_with_the_model_assessor_waits_for_the_decisions(self):
        """The loop used to exit after its first tick: nothing was running and the outbox was empty, while
        every candidate was still waiting for the model. `decisions` was then empty."""
        inbox = os.path.join(self.home, "inbox")
        os.makedirs(inbox)
        for i, text in enumerate(("the export fails", "lunch?"), 1):
            path = os.path.join(inbox, f"{i:03d}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"id": f"e{i}", "sender": "alice", "thread": f"t{i}", "text": text, "addressed": False}, f)
            os.utime(path, (time.time() - 10, time.time() - 10))
        self.llm.script = [{"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": "a", "reason": "r"},
                                                            {"trigger_id": 2, "rule": None, "reason": "r"}]})},
            {"text": json.dumps({"decisions": [{"trigger_id": 1, "rule": "a", "reason": "r"}]})}]
        rules = [x for r in RULES for x in ("--rule", r)]
        done = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "superagent", "--home", self.home, "run", "--once", "--allow", "alice",
             "--assessor", "model", *rules, "--base-url", self.llm.base_url, "--model", "fake-model", "--backoff", "0.01",
             "--interval", "0.1", "--batch-wait", "0.3"],
            capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, PYTHONPATH=sup.SRC), timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        code, out, err = run(["--home", self.home, "decisions", "--jsonl"])
        # the no-rule answer is decided at the first look, the match only after its second look: the list is in decision order
        self.assertEqual([json.loads(x)["model_rule"] for x in out.splitlines()], ["", "a"])
        self.assertEqual([n for n in os.listdir(inbox) if n.endswith(".json")], [], "every event file was taken")

    def test_run_once_gives_up_when_the_model_session_cannot_start(self):
        """With --assessor model every candidate waits for the model session. If that session can never start
        (preflight or process-start error) nothing will ever judge them, and `--once` used to wait forever
        (the review of PR #16 found it). It must stop, say why, exit 1, and leave the candidates pending so a
        later run can still judge them."""
        inbox = os.path.join(self.home, "inbox")
        os.makedirs(inbox)
        path = os.path.join(inbox, "001.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"id": "e1", "sender": "alice", "thread": "t1", "text": "the export fails",
                       "addressed": False}, f)
        os.utime(path, (time.time() - 10, time.time() - 10))
        ticks, real_tick = [], Engine.tick

        def counted_tick(engine):
            ticks.append(1)
            if len(ticks) > 300:                      # a loop that never ends is a failure, not a hung test run
                raise AssertionError("run --once was still waiting after 300 ticks")
            return real_tick(engine)

        def cannot_start(session):
            session.last_error = "OSError: simulated start failure"
            return False

        rules = [x for r in RULES for x in ("--rule", r)]
        with mock.patch.object(Engine, "tick", counted_tick), \
                mock.patch.object(MainSession, "ensure_running", cannot_start):
            code, out, err = run(["--home", self.home, "run", "--once", "--allow", "alice", "--assessor", "model",
                                  *rules, "--base-url", "http://127.0.0.1:9/v1", "--model", "fake-model",
                                  "--interval", "0.01", "--batch-wait", "0.1"])
        self.assertEqual(code, 1)
        self.assertIn("simulated start failure", err)
        self.assertIn("1 message", err)
        store = Store(self.home).open()
        try:
            self.assertEqual(store.eval_counts()["pending"], 1, "the candidate is still waiting for a later run")
        finally:
            store.close()

    def test_what_run_once_waits_for_is_each_of_its_four_things(self):
        """work_left on stand-ins: a running ticket, an undelivered reply, an event file still in the inbox, a
        candidate pending or in flight with the model. Only when all are clear may `--once` exit."""
        from types import SimpleNamespace
        from superagent.__main__ import work_left

        def state(runs=(), outbox=(), files=(), pending=0, sent=0, folder=True):
            d = self.dir if folder else None
            for n in os.listdir(self.dir):
                os.remove(os.path.join(self.dir, n))
            for n in files:
                open(os.path.join(self.dir, n), "w").close()
            engine = SimpleNamespace(runs=list(runs), source=SimpleNamespace(folder=d) if folder else object())
            store = SimpleNamespace(outbox_rows=lambda status: list(outbox),
                                    eval_counts=lambda: {"pending": pending, "sent": sent, "done": 0, "unavailable": 0})
            return work_left(engine, store)

        self.assertFalse(state())
        self.assertFalse(state(files=("readme.txt",)), "only event files count")
        self.assertFalse(state(folder=False), "a source without a folder has nothing to wait for")
        for why, kw in (("a running ticket", dict(runs=[1])), ("an undelivered reply", dict(outbox=[1])),
                        ("an event file in the inbox", dict(files=("001.json",))),
                        ("a candidate waiting for the model", dict(pending=1)),
                        ("a batch in flight", dict(sent=1))):
            with self.subTest(why):
                self.assertTrue(state(**kw))

    def test_work_left_without_the_model_ignores_only_the_model_part(self):
        """When the model session cannot start, `--once` must still wait for what does not need the model (a
        running ticket, a reply to deliver, an event file) but not for candidates only the model can judge."""
        from types import SimpleNamespace
        from superagent.__main__ import work_left
        store = SimpleNamespace(outbox_rows=lambda status: [],
                                eval_counts=lambda: {"pending": 2, "sent": 1, "done": 0, "unavailable": 0})
        engine = SimpleNamespace(runs=[], source=object())
        self.assertTrue(work_left(engine, store))
        self.assertFalse(work_left(engine, store, model=False))
        engine.runs = [1]
        self.assertTrue(work_left(engine, store, model=False))

    def test_a_pipe_closed_under_a_pump_thread_ends_quietly(self):
        """stop() closes a child's pipe while the pump thread reads it; on Windows that raised ValueError
        ('PyMemoryView_FromBuffer(): info->buf must not be NULL') as a traceback on stderr."""
        from superagent.executor import HarnessExecutor

        def pipe(error):
            yield b"first\n"
            yield b"second\n"
            raise error

        for error in (ValueError("PyMemoryView_FromBuffer(): info->buf must not be NULL"), OSError("closed")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(list(HarnessExecutor._lines(pipe(error))), [b"first\n", b"second\n"])
        self.assertEqual(list(HarnessExecutor._lines(iter([b"a\n"]))), [b"a\n"])

    @unittest.skipUnless(os.name == "nt", "the PowerShell code page is a Windows matter")
    def test_a_command_line_with_non_ascii_text_is_read_back_whole(self):
        """`down` printed UnicodeDecodeError from a reader thread: PowerShell answered in the console code
        page (cp950 here) and the answer was decoded as UTF-8."""
        from superagent import procutil
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "中文參數"],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            line = procutil.cmdline(child.pid)
            self.assertIn("中文參數", line or "")
        finally:
            child.kill()
            child.wait()


class TestPumpsReadThroughTheQuietWrapper(unittest.TestCase):
    """PR #16 mutation re-run: bypassing `_lines` in either pump thread left every test green, because the
    existing test only calls `_lines` itself. These run the real pump methods."""

    @staticmethod
    def proc(stdout, stderr):
        from types import SimpleNamespace
        return SimpleNamespace(pid=4242, stdout=stdout, stderr=stderr, wait=lambda: 0)

    @staticmethod
    def broken_pipe(lines, error):
        yield from lines
        raise error

    def drain(self, ex):
        out = []
        while not ex.events.empty():
            out.append(ex.events.get_nowait())
        return out

    def test_stdout_pump_ends_with_an_exit_event_when_the_pipe_is_closed_under_it(self):
        """stop() closes the pipe while this thread reads it; the thread must still report the exit."""
        from superagent.executor import HarnessExecutor
        ex = HarnessExecutor(cfg=None)
        pipe = self.broken_pipe([b'{"kind": "hello"}\n'], ValueError("PyMemoryView_FromBuffer(): info->buf must not be NULL"))
        ex._pump_out(self.proc(pipe, iter([])))
        self.assertEqual([e["kind"] for e in self.drain(ex)], ["hello", "exited"])

    def test_stderr_pump_keeps_what_it_read_when_the_pipe_is_closed_under_it(self):
        from superagent.executor import HarnessExecutor
        ex = HarnessExecutor(cfg=None)
        pipe = self.broken_pipe([b"warning\n"], OSError("closed"))
        ex._pump_err(self.proc(iter([]), pipe))
        self.assertEqual([(e["kind"], e["text"]) for e in self.drain(ex)], [("stderr", "warning")])


@unittest.skipUnless(os.name == "nt", "the PowerShell scan is a Windows matter")
class TestScanByCommandLine(unittest.TestCase):
    """`find_by_cmdline` is the emergency path that finds our harness children when the database cannot be
    trusted (lifecycle.kill_all). A fragment is pasted into a PowerShell script, so what PowerShell reads as a
    quote matters: it treats the typographic quotes U+2018..U+201B like the plain one."""

    FRAGMENTS = ("plain", "it's", "Roy’s files", "left‘quote", "low‚quote", "rev‛quote",
                 "中文參數", "$dollar", "back`tick")

    def test_a_process_is_found_whatever_quotes_or_letters_its_command_line_holds(self):
        from superagent import procutil
        for fragment in self.FRAGMENTS:
            with self.subTest(fragment=fragment):
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", fragment],
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    self.assertIn(child.pid, procutil.find_by_cmdline("time.sleep(60)", fragment))
                finally:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    unittest.main()
