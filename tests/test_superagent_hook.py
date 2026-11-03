"""Checkpoint D: the mandate hook's policy, one row per tool, plus its process contract.

Each test's docstring says WHY the behaviour matters.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import superagent_support as sup
from superagent import mandate_hook as mh
from test_superagent_boundary import RealProcess

HOOK = mh.__file__


class Policy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(os.path.join(self._tmp.name, "ticket"))
        os.makedirs(os.path.join(self.root, "sub"))
        self.outside = os.path.realpath(os.path.join(self._tmp.name, "secret"))
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "s.txt"), "w") as f:
            f.write("secret")
        self.mandate = {"root": self.root}

    def tearDown(self):
        self._tmp.cleanup()

    def decide(self, tool, args, cwd=None):
        return mh.decide(tool, args, self.mandate, cwd=self.root if cwd is None else cwd)

    def test_every_tool_the_harness_offers_has_an_explicit_row(self):
        """If the harness gains a tool, the hook must not treat it as 'unknown, vetoed' by
        accident: someone has to decide whether a ticket gets it. Names come from the real
        harness (a child process, so its .harness.env loading cannot touch this one)."""
        code = ("import json, sys; sys.path.insert(0, %r); import harness_vllm as hv; "
                "print(json.dumps([t['name'] for t in hv.tools_for(0)] + "
                "[hv.harness_skills.SKILL_TOOL['name']]))" % os.path.join(sup.SRC, "harness"))
        env = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}
        env.update(HARNESS_VISION="1", HARNESS_PROVIDER="vllm", HARNESS_BASE_URL="http://127.0.0.1:9/v1",
                   HARNESS_MODEL="m")
        with tempfile.TemporaryDirectory() as d:
            out = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True,
                                 encoding="utf-8", cwd=d, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        names = set(json.loads(out.stdout))
        known = mh.DENY | mh.PASS | mh.CHECK_PATH | {"run_command"}
        self.assertEqual(sorted(names - known), [], "a harness tool has no row in the mandate policy")
        self.assertEqual(sorted((mh.DENY | mh.PASS | mh.CHECK_PATH | {"run_command"}) - names), [],
                         "the policy lists a tool the harness does not have")

    def test_denied_tools_are_refused_by_name(self):
        """Each refusal must be about THIS tool, not the catch-all for unknown names: the
        catch-all would hide a deleted row."""
        for tool in sorted(mh.DENY):
            with self.subTest(tool=tool):
                reason = self.decide(tool, {})
                self.assertIsNotNone(reason)
                self.assertIn("not part of this ticket's mandate", reason)

    def test_mcp_tools_are_refused(self):
        for tool in ("files__read", "corp__create_issue", "a__b__c"):
            with self.subTest(tool=tool):
                self.assertIn("mandate", self.decide(tool, {}))

    def test_unknown_tool_is_refused(self):
        self.assertIn("unknown tool", self.decide("frobnicate", {}))

    def test_todo_write_is_allowed(self):
        self.assertIsNone(self.decide("todo_write", {"todos": []}))

    def test_paths(self):
        """Reads are not asked about by `safe`, so the hook is the only thing between the model
        and every file on the machine. Writes are asked about, but outside the folder is
        refused before anyone is bothered."""
        root = self.root
        inside = [{"path": "a.txt"}, {"path": "sub/b.txt"}, {"path": os.path.join(root, "c.txt")},
                  {"path": "./sub/../d.txt"}]
        outside = [{"path": "../x"}, {"path": "sub/../../x"}, {"path": os.path.join(self.outside, "s.txt")},
                   {"path": "C:/Windows/win.ini"}, {"path": ""}, {}, {"path": None}, {"path": 5}]
        for tool in ("read_file", "write_file", "edit_file"):
            for args in inside:
                with self.subTest(tool=tool, args=args, expect="allow"):
                    self.assertIsNone(self.decide(tool, args))
            for args in outside:
                with self.subTest(tool=tool, args=args, expect="refuse"):
                    self.assertIn("outside the ticket folder", self.decide(tool, args))

    def test_path_comparison_ignores_case(self):
        """Windows paths are case-insensitive: C:\\USERS\\... is the same folder."""
        self.assertIsNone(self.decide("read_file", {"path": os.path.join(self.root.upper(), "a.txt")}))
        self.assertIn("outside", self.decide("read_file", {"path": os.path.join(self.outside.upper(), "s.txt")}))

    @unittest.skipUnless(os.name == "nt", "directory junctions are a Windows feature")
    def test_a_junction_inside_the_folder_does_not_lead_out(self):
        """A link inside the ticket folder that points outside must not widen the mandate; the
        comparison is on real paths. (Making the link needs a shell command, which `safe`
        would ask about, so this is a second line of defence.)"""
        link = os.path.join(self.root, "link")
        out = subprocess.run(["cmd", "/c", "mklink", "/J", link, self.outside], capture_output=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("outside", self.decide("read_file", {"path": "link/s.txt"}))
        self.assertIn("outside", self.decide("write_file", {"path": "link/new.txt"}))

    def test_run_command_background_is_refused(self):
        """A background job finishing starts a harness turn nobody sent, and the one-input-in-
        flight rule only works while every turn_end belongs to our input."""
        for value in ("true", "True", "TRUE", True, "1", "yes", "anything"):
            with self.subTest(value=value):
                self.assertIn("background", self.decide("run_command", {"command": "dir", "background": value}))
        for value in (None, "", False, "false", "False", "0", "no"):
            with self.subTest(value=value):
                self.assertIsNone(self.decide("run_command", {"command": "dir", "background": value}))
        self.assertIsNone(self.decide("run_command", {"command": "dir"}))

    def test_cwd_must_be_the_ticket_folder(self):
        """If the harness ever ran somewhere else, 'inside the ticket folder' would be measured
        from the wrong place."""
        self.assertIn("not in the ticket folder", self.decide("read_file", {"path": "a"}, cwd=self.outside))

    def test_a_payload_without_cwd_is_refused(self):
        """The folder check measures from the harness' real working folder. If the field ever
        goes missing, the safe reading is 'cannot tell', not 'fine'."""
        for cwd in (None, "", 5):
            with self.subTest(cwd=cwd):
                self.assertIn("no cwd", mh.decide("todo_write", {}, self.mandate, cwd=cwd))
                self.assertIn("no cwd", mh.decide("run_command", {"command": "dir"}, self.mandate, cwd=cwd))

    def test_bad_inputs_fail_closed(self):
        self.assertIsNotNone(mh.decide("read_file", {"path": "a"}, {}, cwd=None))
        self.assertIsNotNone(mh.decide("read_file", {"path": "a"}, {"root": ""}, cwd=None))
        for bad in (None, [], "x", 3):
            with self.subTest(tool_input=bad):
                self.assertIn("not an object", self.decide("read_file", bad))


class Contract(unittest.TestCase):
    """The hook as the harness runs it: a process, JSON on stdin, an exit code."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.mandate = os.path.join(self.root, "..", "m-%d.json" % os.getpid())
        with open(self.mandate, "w", encoding="utf-8") as f:
            json.dump({"root": self.root}, f)

    def tearDown(self):
        os.remove(self.mandate)
        self._tmp.cleanup()

    def run_hook(self, payload, mandate=None, stdin=None, args=None):
        argv = [sys.executable, "-X", "utf8", HOOK] + (args if args is not None
                                                       else ["--mandate", mandate or self.mandate])
        return subprocess.run(argv, input=stdin if stdin is not None else json.dumps(payload),
                              capture_output=True, text=True, encoding="utf-8")

    def payload(self, tool, args):
        return {"tool_name": tool, "tool_input": args, "cwd": self.root}

    def test_allowed_exits_0_and_is_silent(self):
        r = self.run_hook(self.payload("read_file", {"path": "a.txt"}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_refused_exits_2_with_a_reason_on_stderr(self):
        r = self.run_hook(self.payload("agent", {"task": "x"}))
        self.assertEqual(r.returncode, 2)
        self.assertIn("superagent mandate", r.stderr)

    def test_anything_it_cannot_decide_is_a_veto(self):
        """A hook that crashes into 'allow' would turn every parsing slip into an open door."""
        cases = {
            "missing mandate file": dict(payload=self.payload("read_file", {"path": "a"}),
                                         mandate=os.path.join(self.root, "nope.json")),
            "stdin is not json": dict(payload=None, stdin="{not json"),
            "stdin is empty": dict(payload=None, stdin=""),
            "no arguments": dict(payload=None, stdin="{}", args=[]),
            "wrong flag": dict(payload=None, stdin="{}", args=["--other", "x"]),
        }
        for name, kw in cases.items():
            with self.subTest(case=name):
                r = self.run_hook(**kw)
                self.assertEqual(r.returncode, 2, r.stderr)

    def test_payload_without_cwd_exits_2(self):
        r = self.run_hook({"tool_name": "todo_write", "tool_input": {}})
        self.assertEqual(r.returncode, 2)
        self.assertIn("no cwd", r.stderr)

    def test_corrupt_mandate_is_a_veto(self):
        with open(self.mandate, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(self.run_hook(self.payload("todo_write", {})).returncode, 2)


class RealHarness(RealProcess):
    def test_corrupt_mandate_blocks_in_the_real_harness(self):
        """The same fail-closed behaviour, end to end: the harness must report the block to the
        model, and the tool must not run."""
        self.boot([{"calls": [("todo_write", {"todos": []})]}, {"text": "ok"}], mandate=False)
        with open(self.mandate, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.exec.send("go")
        res, _ = sup.wait_for(self.exec.events, "tool_result")
        self.assertIn("BLOCKED by hook", res["text"])

    def test_policy_rows_reach_the_model_as_refusals(self):
        """Every denied tool, asked for through the real harness, comes back as a refusal and the
        turn ends normally: a refusal is information for the model, not an error state."""
        calls = [("agent", {"task": "x"}), ("skill", {"name": "x"}), ("view_image", {"path": "a.png"}),
                 ("monitor", {"command": "dir"}), ("schedule_wakeup", {"seconds": 5, "reason": "r"}),
                 ("task_output", {"id": "b1"}), ("task_stop", {"id": "b1"}), ("ask_user", {"question": "?"}),
                 ("run_command", {"command": "dir", "background": "true"}),
                 ("read_file", {"path": "C:/Windows/win.ini"})]
        self.boot([{"calls": calls}, {"text": "all refused"}])
        self.exec.send("try everything")
        results, deadline = [], 0
        while len(results) < len(calls):
            ev, _ = sup.wait_for(self.exec.events, "tool_result", timeout=30)
            results.append(ev["text"])
        for call, text in zip(calls, results):
            with self.subTest(tool=call[0]):
                self.assertIn("BLOCKED by hook", text)
        sup.wait_for(self.exec.events, "turn_end")


if __name__ == "__main__":
    unittest.main()
