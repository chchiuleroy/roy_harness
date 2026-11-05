"""Checkpoint F: the control channel, the command line, and the three ways of taking it away.

Each test's docstring says WHY the behaviour matters.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import superagent_support as sup
from superagent import lifecycle, procutil
from superagent.control import Channel
from superagent.engine import Engine
from superagent.gate import Policy, RuleAssessor
from superagent.model import Strength
from superagent.store import Store
from test_superagent_engine import Rig

REPO = os.path.normpath(os.path.join(sup.HERE, ".."))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def sleeper(*extra_arg):
    """A harmless long-lived process whose command line contains the given words."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", *extra_arg],
                            creationflags=NO_WINDOW)


def reap(*procs):
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()


def not_green(outcomes):
    """Tests of a suite that did not pass. A removal check compares two suites; if the baseline
    itself is not green, 'identical' proves little (a different failure would look the same)."""
    return sorted(name for name, outcome in outcomes.items()
                  if outcome not in ("ok", "skipped", "expected failure"))


def without_superagent_tests(outcomes):
    """The outcomes of the tests that are still there once the superagent test files are deleted
    (the removal copy deletes every tests/ file with 'superagent' in its name, so match the same way)."""
    return {name: o for name, o in outcomes.items() if "superagent" not in name.split(".")[0]}


def passed_count(outcomes):
    """How many tests actually passed (skipped ones did not run anything)."""
    return sum(1 for o in outcomes.values() if o == "ok")


def suite_env(environ):
    """The environment for a nested suite run. The full tree now contains this very test, so the
    nested run must not see SUPERAGENT_REMOVAL (it would start another nested run, forever); the
    live switches go too, so both sides of the comparison are the offline suite."""
    return {k: v for k, v in environ.items() if k not in ("HARNESS_LIVE", "SUPERAGENT_LIVE", "SUPERAGENT_REMOVAL")}


class TestRemovalGuard(unittest.TestCase):
    def test_a_suite_that_mostly_skipped_does_not_count_as_having_run(self):
        """'Identical' is only worth something if the tests ran. Skipped counts as green in
        not_green, so a suite that discovered 200 tests and skipped them all would otherwise
        pass the comparison without any harness code having been executed."""
        self.assertEqual(passed_count({"a": "ok", "b": "skipped", "c": "FAIL", "d": "ok"}), 2)
        self.assertEqual(passed_count({name: "skipped" for name in "abcdef"}), 0)

    def test_a_nested_run_cannot_start_another_removal_check(self):
        env = suite_env({"SUPERAGENT_REMOVAL": "1", "SUPERAGENT_LIVE": "1", "HARNESS_LIVE": "1", "PATH": "p"})
        self.assertEqual(env, {"PATH": "p"})

    def test_a_baseline_that_is_not_green_is_reported(self):
        self.assertEqual(not_green({"a": "ok", "b": "skipped"}), [])
        self.assertEqual(not_green({"a": "ok", "b": "FAIL", "c": "ERROR"}), ["b", "c"])
        self.assertEqual(not_green({"unittest.loader._FailedTest.x": "ERROR"}),
                         ["unittest.loader._FailedTest.x"])

    def test_only_the_tests_that_survive_removal_are_compared(self):
        """The removal check runs the whole suite, then compares what is left once the superagent
        tests are dropped. If the filter kept a superagent test, the copy without superagent could
        never match it; if it dropped a harness test, a harness test that needs superagent would
        slip through. Matching on the module name (the first dotted part of the id) is what the
        copy does when it deletes files with 'superagent' in their name."""
        got = without_superagent_tests({
            "test_harness_vllm.TestA.test_x": "ok",
            "test_harness_gui.TestB.test_y": "FAIL",
            "test_superagent_cli.TestC.test_z": "ok",
            "test_superagent_engine.TestD.test_w": "ok"})
        self.assertEqual(got, {"test_harness_vllm.TestA.test_x": "ok",
                               "test_harness_gui.TestB.test_y": "FAIL"})


class TestControlChannel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ch = Channel(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_commands_come_out_in_order_and_only_once(self):
        a = self.ch.submit("stop", ticket=1)
        b = self.ch.submit("answer", ask_id=7, yes=False)
        got = self.ch.take()
        self.assertEqual([n for n, _ in got], [a, b])
        self.assertEqual(got[1][1], {"cmd": "answer", "ask_id": 7, "yes": False})
        self.assertEqual(self.ch.take(), [])

    def test_reply_reaches_the_waiting_caller(self):
        name = self.ch.submit("resume", ticket=3)
        self.ch.take()
        self.ch.reply(name, True, "resumed")
        self.assertEqual(self.ch.wait(name, 2), {"ok": True, "message": "resumed"})

    def test_no_reply_means_none_not_success(self):
        """A CLI that prints 'done' when nobody was there to do it would be a lie."""
        self.assertIsNone(self.ch.wait(self.ch.submit("stop", ticket=1), 0.3))

    def test_unknown_command_is_refused_at_the_door(self):
        with self.assertRaises(ValueError):
            self.ch.submit("rm-rf")

    def test_a_half_written_command_is_never_taken(self):
        """Commands are written to a temp name and renamed, so the engine cannot read half of one."""
        with open(os.path.join(self.ch.dir, ".x.tmp"), "w") as f:
            f.write('{"cmd": "stop"')
        self.assertEqual(self.ch.take(), [])


class TestStopProcesses(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.home)
        self.procs = []

    def tearDown(self):
        reap(*self.procs)
        self._tmp.cleanup()

    def test_scan_finds_ours_by_command_line_and_leaves_others_alone(self):
        """The emergency path cannot rely on the database, so it looks at command lines. It
        must kill the one that names our home and nothing else."""
        # looks like one of our harness children: its session file is under the home
        ours = sleeper("harness_vllm.py", "--jsonl", os.path.join(self.home, "tickets", "1", "session.json"))
        other = sleeper("unrelated-marker")
        # mentions the home but is not a harness child (an editor, a backup tool, a shell)
        bystander = sleeper("notepad.exe", os.path.join(self.home, "tickets", "1", "work", "notes.txt"))
        self.procs += [ours, other, bystander]
        time.sleep(0.5)
        killed = lifecycle.stop_processes(self.home)
        self.assertIn(ours.pid, killed)
        self.assertNotIn(other.pid, killed)
        self.assertNotIn(bystander.pid, killed)
        self.assertIsNotNone(procutil.create_time(other.pid), "an unrelated process was killed")
        self.assertIsNotNone(procutil.create_time(bystander.pid),
                             "a process that only mentions the home folder was killed")
        self.assertIsNone(procutil.create_time(ours.pid))

    def test_a_reused_pid_in_the_manifest_is_not_killed(self):
        """The manifest says pid N started at time T. If N now belongs to some other program
        (different start time, no home in its command line) it must be left alone."""
        victim = sleeper("innocent-bystander")
        self.procs.append(victim)
        with Store(self.home) as s:
            tid, _ = s.new_ticket("s", "t", Strength.EXPLICIT, lambda i: ("c", "s", "c"))
            s.start_run(tid, victim.pid, 12345.0, "c")            # a start time that is not the real one
        lifecycle.stop_processes(self.home)
        self.assertIsNotNone(procutil.create_time(victim.pid), "a process with a reused pid was killed")


class TestThreeMeanings(Rig):
    def running_ticket(self):
        self.llm.script[:] = [{"calls": [("run_command", {"command": "echo x"})]}]
        self.drop("001.json")
        self.until(lambda: self.engine.store.open_asks(), what="a question")
        return self.engine.runs[1].pid

    def test_stop_ends_processes_and_keeps_the_data(self):
        pid = self.running_ticket()
        self.stores[0].close()                                  # the orchestrator is gone
        out = lifecycle.down(self.home, "stop")
        self.assertIn(pid, out["killed"])
        self.assertIsNone(procutil.create_time(pid))
        self.assertTrue(os.path.exists(os.path.join(self.home, "state.sqlite")))
        self.assertFalse(out["removed"])

    def test_purge_leaves_nothing_behind(self):
        """'Completely removed' is checked, not assumed: no folder, no process whose command
        line or manifest entry names the home."""
        pid = self.running_ticket()
        self.stores[0].close()
        out = lifecycle.down(self.home, "purge")
        self.assertTrue(out["removed"])
        self.assertFalse(os.path.exists(self.home))
        self.assertIsNone(procutil.create_time(pid))
        self.assertEqual(procutil.find_by_cmdline(self.home), [])

    def test_purge_works_when_the_database_is_garbage(self):
        """The way out must not depend on the thing that may be broken."""
        pid = self.running_ticket()
        self.stores[0].close()
        with open(os.path.join(self.home, "state.sqlite"), "wb") as f:
            f.write(os.urandom(4096))
        lifecycle.down(self.home, "purge")
        self.assertFalse(os.path.exists(self.home))
        self.assertIsNone(procutil.create_time(pid), "the process survived because the manifest was unreadable")


class TestCommandLine(Rig):
    """The real thing: `python -m superagent run` as a process, driven only through the CLI."""

    def cli(self, *args, timeout=60):
        env = dict(os.environ, PYTHONPATH=sup.SRC)
        return subprocess.run([sys.executable, "-X", "utf8", "-m", "superagent", "--home", self.home, *args],
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=timeout,
                              creationflags=NO_WINDOW)

    def test_run_answer_status_and_purge_from_the_command_line(self):
        for st in self.stores:
            st.close()                                           # the process below takes the lock
        self.llm.script[:] = [{"calls": [("run_command", {"command": "echo marker> ran.txt"})]},
                              {"text": "finished"}]
        self.drop("001.json", text="please run it")
        env = dict(os.environ, PYTHONPATH=sup.SRC)
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "superagent", "--home", self.home, "run", "--allow", "alice",
             "--inbox", self.inbox_dir, "--outbox", self.out_dir, "--provider", "vllm",
             "--base-url", self.llm.base_url, "--model", "fake", "--backoff", "0.01", "--interval", "0.2"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, creationflags=NO_WINDOW)
        try:
            deadline, status = time.time() + 60, {}
            while time.time() < deadline:
                r = self.cli("status")
                if r.returncode == 0:
                    status = json.loads(r.stdout)
                    if status["open_asks"]:
                        break
                time.sleep(0.5)
            self.assertTrue(status.get("open_asks"), f"no question appeared; {status} {proc.poll()}")
            self.assertTrue(status["running"])
            ask_id = status["open_asks"][0]["ask_id"]
            self.assertEqual(self.cli("answer", "999", "y").returncode, 1, "an unknown question was accepted")
            self.assertEqual(self.cli("answer", str(ask_id), "y").returncode, 0)
            cwd = os.path.join(self.home, "tickets", "1", "work")
            deadline = time.time() + 30
            while not os.path.exists(os.path.join(cwd, "ran.txt")) and time.time() < deadline:
                time.sleep(0.2)
            self.assertTrue(os.path.exists(os.path.join(cwd, "ran.txt")), "the approved command did not run")
            r = self.cli("down", "--mode", "purge", timeout=90)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(self.home))
            proc.wait(30)
            self.assertEqual(procutil.find_by_cmdline(self.home), [])
        finally:
            reap(proc)
            for pipe in (proc.stdout, proc.stderr):
                pipe.close()

    def test_run_refuses_a_second_orchestrator_on_the_same_home(self):
        for st in self.stores:
            st.close()
        with Store(self.home):
            r = self.cli("run", "--base-url", "http://127.0.0.1:9/v1", "--model", "m")
        self.assertEqual(r.returncode, 3)
        self.assertIn("in use", r.stderr)


@unittest.skipUnless(os.environ.get("SUPERAGENT_REMOVAL") == "1",
                     "slow: set SUPERAGENT_REMOVAL=1 to run the removal check")
class TestRemovable(unittest.TestCase):
    """C1a: the harness's own tests give the same results with the superagent folder and its tests
    deleted as they do in the full tree. (This used to also pin src/harness to one commit; that
    made the check go red the first time anyone legitimately changed the harness, and it measured
    'nobody touched the harness' rather than 'the harness does not need superagent'.)"""

    def run_suite(self, root):
        """{test id: outcome} for the suite in `root`, from a runner that records nothing else."""
        runner = os.path.join(sup.HERE, "superagent_removal_runner.py")
        r = subprocess.run([sys.executable, "-X", "utf8", runner, root], capture_output=True, text=True,
                           encoding="utf-8", env=suite_env(os.environ))
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
        self.assertTrue(lines, r.stderr[-2000:])
        return json.loads(lines[-1])

    def test_suite_identical_without_superagent(self):
        with tempfile.TemporaryDirectory() as full, tempfile.TemporaryDirectory() as cut:
            for root in (full, cut):
                for name in ("src", "tests", "bench"):
                    if os.path.isdir(os.path.join(REPO, name)):
                        shutil.copytree(os.path.join(REPO, name), os.path.join(root, name),
                                        ignore=shutil.ignore_patterns("__pycache__"))
            shutil.rmtree(os.path.join(cut, "src", "superagent"))
            for f in os.listdir(os.path.join(cut, "tests")):
                if "superagent" in f:
                    os.remove(os.path.join(cut, "tests", f))
            before = without_superagent_tests(self.run_suite(full))
            after = self.run_suite(cut)
        self.assertGreater(passed_count(before), 50, "the full-tree suite did not run (skipped tests ran nothing)")
        self.assertEqual(not_green(before), [], "the full-tree suite is not green, so 'identical' proves little")
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
