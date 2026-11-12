"""Checkpoint C: the executor against a real harness child process: questions, answers,
process trees and what is recorded about the process.

Each test's docstring says WHY the behaviour matters.
"""
import json
import os
import sys
import tempfile
import time
import unittest

import superagent_support as sup
from superagent import executor as ex
from superagent import procutil
from test_superagent_boundary import RealProcess


class TestAnswer(unittest.TestCase):
    def test_answer_never_writes_anything_but_y_or_n(self):
        """An `a` answer would be saved by the harness as a PERMANENT grant (PERMS), after which
        `safe` mode stops asking for that command. The port must not be able to produce one."""
        sent = []
        e = ex.HarnessExecutor(ex.ExecutorConfig("vllm", "http://example.com/v1", "m"))
        e._send_raw = lambda obj: sent.append(obj) or True
        e.answer("q1", True)
        e.answer("q2", False)
        e.answer("q3", "a")                      # truthy junk must not turn into "a"
        self.assertEqual([o["text"] for o in sent], ["y", "n", "y"])
        self.assertTrue(all(o["type"] == "answer" for o in sent))


class TestChildEnvironment(unittest.TestCase):
    def test_a_ticket_never_gets_the_notes_feature(self):
        """Notes are on by default in the harness, but a ticket works on outside text with nobody
        watching: it must not be able to save notes, whatever the parent's environment or a
        `.harness.env` says."""
        cfg = ex.ExecutorConfig("vllm", "http://example.com/v1", "m")
        for parent in ({}, {"HARNESS_NOTES": "1"}):
            self.assertEqual(ex.build_env(cfg, parent=parent)["HARNESS_NOTES"], "0", parent)
        self.assertIn("HARNESS_NOTES", ex.pinned_keys())       # a .harness.env cannot set it either


class TestEventNumbers(unittest.TestCase):
    """The sequence numbers that tell an answer's consequences from events that came before it."""

    def fresh(self):
        e = ex.HarnessExecutor(ex.ExecutorConfig("vllm", "http://example.com/v1", "m"))
        e.sent = []
        e._send_raw = lambda obj: e.sent.append(obj) or True
        return e

    def test_events_are_numbered_in_queue_order(self):
        e = self.fresh()
        for k in ("a", "b", "c"):
            e._emit({"kind": k})
        got = [e.events.get_nowait() for _ in range(3)]
        self.assertEqual([g["_seq"] for g in got], [1, 2, 3])

    def test_the_mark_separates_earlier_events_from_later_ones(self):
        e = self.fresh()
        e._emit({"kind": "tool_result"})
        e.answer("q1", True)
        e._emit({"kind": "tool_result"})
        first, second = e.events.get_nowait(), e.events.get_nowait()
        self.assertLessEqual(first["_seq"], e.last_answer_mark)
        self.assertGreater(second["_seq"], e.last_answer_mark)

    def test_nothing_can_be_queued_between_noting_the_mark_and_writing(self):
        """Found by review: counting the queue and then writing left a gap in which a reader
        thread could queue an older event that the count did not include. The mark and the write
        are now one locked step: an event offered during the write waits, and gets a later number."""
        import threading
        e = self.fresh()
        late = threading.Thread(target=lambda: e._emit({"kind": "tool_result", "text": "during"}))
        seen_during = []

        def write(obj):
            late.start()
            late.join(0.3)                                # it must still be waiting for the lock
            seen_during.append(e.events.qsize())
            return True
        e._send_raw = write
        e.answer("q1", True)
        late.join(5)
        self.assertEqual(seen_during, [0], "an event was queued while the answer was being written")
        ev = e.events.get_nowait()
        self.assertGreater(ev["_seq"], e.last_answer_mark)

    def test_an_answer_that_fails_to_write_still_reports_false(self):
        e = self.fresh()
        e._send_raw = lambda obj: False
        self.assertFalse(e.answer("q1", True))


class TestQuestions(RealProcess):
    def ask_for_command(self, command="echo superagent-ok"):
        self.boot([{"calls": [("run_command", {"command": command})]}, {"text": "finished"}])
        self.exec.send("run something")
        ev, _ = sup.wait_for(self.exec.events, "ask")
        return ev

    def test_safe_mode_asks_before_a_command(self):
        """The whole first-stage safety story rests on this question being raised and on the
        process waiting for it."""
        ev = self.ask_for_command()
        self.assertIn("echo superagent-ok", ev["what"])
        self.assertTrue(ev["rid"].startswith("q"))
        self.assertTrue(self.exec.alive)

    def test_no_answer_means_no_command(self):
        """While a question is open nothing runs: the child just waits."""
        self.ask_for_command("echo marker > ran.txt")
        time.sleep(1.0)
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "ran.txt")))

    def test_answering_no_denies(self):
        ev = self.ask_for_command("echo marker > ran.txt")
        self.exec.answer(ev["rid"], False)
        res, _ = sup.wait_for(self.exec.events, "tool_result")
        self.assertIn("DENIED", res["text"])
        sup.wait_for(self.exec.events, "turn_end")
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "ran.txt")))

    def test_answering_yes_runs_only_that_command(self):
        ev = self.ask_for_command("echo marker> ran.txt")
        self.exec.answer(ev["rid"], True)
        res, _ = sup.wait_for(self.exec.events, "tool_result")
        self.assertNotIn("DENIED", res["text"])
        sup.wait_for(self.exec.events, "turn_end")
        self.assertTrue(os.path.exists(os.path.join(self.cwd, "ran.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.cwd, ".harness_permissions.json")),
                         "a yes was stored as a permanent grant")

    def test_harness_reuses_rid_q1_after_a_restart(self):
        """This is the fact that makes an `ask_id` necessary: the harness numbers its questions
        per process, so the second process asks `q1` again. If this ever stops being true the
        extra identity is merely redundant; while it is true, rid alone would be a bug."""
        first = self.ask_for_command("echo one")
        self.exec.stop(force=True)
        self.llm.script[:] = [{"calls": [("run_command", {"command": "echo two"})]}, {"text": "ok"}]
        again = ex.HarnessExecutor(self.exec.cfg, self.harness)
        again.start(self.cwd, self.session)
        try:
            sup.wait_for(again.events, "ready")
            again.send("go on")
            second, _ = sup.wait_for(again.events, "ask")
        finally:
            again.stop(force=True)
        self.assertEqual(first["rid"], second["rid"])
        self.assertIn("echo two", second["what"])


class TestProcessTree(RealProcess):
    def test_force_stop_ends_grandchildren(self):
        """Windows terminate() leaves grandchildren behind; a ticket's shell command must not
        outlive the ticket. The command writes its own pid so the test can look for it."""
        code = "import os,time;open('gc.pid','w').write(str(os.getpid()));time.sleep(120)"
        cmd = f'python -c "{code}"'      # bare `python`: the hook interpreter's own file name, python.exe,
        #                                    is protected by the harness and refused inside commands
        self.boot([{"calls": [("run_command", {"command": cmd})]}, {"text": "done"}])
        self.exec.send("start a long job")
        ev, _ = sup.wait_for(self.exec.events, "ask")
        self.exec.answer(ev["rid"], True)
        pidfile = os.path.join(self.cwd, "gc.pid")
        deadline = time.time() + 20
        def read_pid():
            try:
                with open(pidfile) as f:
                    return f.read().strip()
            except OSError:
                return ""
        while not read_pid():
            self.assertLess(time.time(), deadline, "the grandchild never started")
            time.sleep(0.1)
        gc_pid = int(read_pid())
        self.assertIsNotNone(procutil.create_time(gc_pid), "grandchild not found before the stop")
        self.exec.stop(force=True)
        deadline = time.time() + 10
        while procutil.create_time(gc_pid) is not None:
            self.assertLess(time.time(), deadline, "the grandchild survived the stop")
            time.sleep(0.1)


class TestProcutil(unittest.TestCase):
    def test_is_ours_checks_start_time_and_command_line(self):
        """A recorded pid can be reused by an unrelated program; `purge` must only kill what it
        started. Start time and a command line naming our home must both match."""
        with tempfile.TemporaryDirectory() as d:
            p = __import__("subprocess").Popen(
                [sys.executable, "-c", "import time;time.sleep(30)", "--jsonl", os.path.join(d, "s.json")],
                creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))
            try:
                t0 = procutil.create_time(p.pid)
                self.assertIsNotNone(t0)
                self.assertTrue(procutil.is_ours(p.pid, t0, d))
                self.assertFalse(procutil.is_ours(p.pid, t0 + 3600, d), "a different start time matched")
                self.assertFalse(procutil.is_ours(p.pid, t0, os.path.join(d, "elsewhere-xyz")),
                                 "a command line without our home matched")
            finally:
                p.kill()
                p.wait()
            self.assertIsNone(procutil.create_time(p.pid))
            self.assertFalse(procutil.is_ours(p.pid, t0, d))


if __name__ == "__main__":
    unittest.main()
