"""Checkpoint 0: the process boundary between superagent and the harness.

These tests start the REAL harness as a child process against a scripted model endpoint. What
they guard is everything that decides what the child may do before any orchestration logic
exists: its environment, its config files, and whether the mandate hook really runs.

Run:  python -X utf8 -m unittest discover -s tests -v
Each test's docstring says WHY the behaviour matters.
"""
import json
import os
import sys
import tempfile
import unittest

import superagent_support as sup
from superagent import executor as ex


def cfg_for(llm, **kw):
    return ex.ExecutorConfig("vllm", llm.base_url, "fake-model", backoff=0.01, **kw)


class TestEnv(unittest.TestCase):
    def test_child_env_is_allowlist_plus_pins(self):
        """The parent's stream setting, endpoint and secrets must not reach the child: a test
        that depends on the machine it runs on proves nothing, and a secret in the child can
        be sent anywhere the model writes a command."""
        parent = {"PATH": "p", "SYSTEMROOT": "C:\\Windows", "FOO_TOKEN": "x", "HARNESS_STREAM": "1",
                  "VLLM_BASE_URL": "http://example.com/v1", "ANTHROPIC_API_KEY": "sk-1",
                  "HARNESS_MODE": "auto", "HARNESS_DEBUG": "1"}
        env = ex.build_env(ex.ExecutorConfig("vllm", "http://example.com/v1", "m"), parent=parent)
        self.assertNotIn("FOO_TOKEN", env)
        self.assertEqual(env["HARNESS_STREAM"], "0")
        self.assertEqual(env["HARNESS_MODE"], "safe")
        self.assertEqual(env["VLLM_BASE_URL"], "")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")
        self.assertEqual(env["HARNESS_DEBUG"], "")
        self.assertEqual(env["PATH"], "p")

    def test_mode_cannot_be_chosen_by_the_caller(self):
        """`normal` does not ask before writing files and `auto` hands approval to a classifier
        that quotes 'the user's words'; a ticket gets neither."""
        cfg = ex.ExecutorConfig("vllm", "http://example.com/v1", "m")
        self.assertEqual(ex.build_env(cfg, parent={"HARNESS_MODE": "auto"})["HARNESS_MODE"], "safe")
        with self.assertRaises(TypeError):
            ex.build_env(cfg, parent={}, extra={"HARNESS_MODE": "auto"})

    def test_api_key_only_when_configured(self):
        """An empty key is a pin; a configured key is the one deliberate exception."""
        base = ex.ExecutorConfig("openai", "https://api.example.com/v1", "m")
        self.assertEqual(ex.build_env(base, parent={})["HARNESS_API_KEY"], "")
        keyed = ex.ExecutorConfig("openai", "https://api.example.com/v1", "m", api_key="k1")
        self.assertEqual(ex.build_env(keyed, parent={})["HARNESS_API_KEY"], "k1")


class TestPreflight(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tree = self._tmp.name
        self.harness = sup.copy_harness_tree(self.tree)
        self.cwd = os.path.join(self.tree, "ticket")
        os.makedirs(self.cwd)

    def tearDown(self):
        self._tmp.cleanup()

    def _env_file(self, text):
        with open(os.path.join(self.tree, ".harness.env"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_unknown_key_in_repo_env_file_refuses_start(self):
        """harness_vllm loads every key of .harness.env with setdefault; a key nobody pinned
        would silently shape the ticket's process."""
        self._env_file("HARNESS_SOMETHING_NEW=1\n")
        with self.assertRaises(ex.PreflightError):
            ex.preflight(self.cwd, self.harness)

    def test_pinned_keys_in_repo_env_file_are_fine(self):
        """The author's own endpoint lives in that file; refusing every file would make the
        layer unusable on the machine it was written for."""
        self._env_file("VLLM_BASE_URL=http://example.com/v1\nHARNESS_PROVIDER=vllm\n# comment\n")
        ex.preflight(self.cwd, self.harness)

    def test_permission_file_in_ticket_folder_refuses_start(self):
        """A permanent grant makes `safe` mode skip the question: granted() is checked first."""
        with open(os.path.join(self.cwd, ".harness_permissions.json"), "w") as f:
            json.dump({"exact": ["whoami"], "prefix": []}, f)
        with self.assertRaises(ex.PreflightError):
            ex.preflight(self.cwd, self.harness)

    def test_env_file_in_ticket_folder_refuses_start(self):
        """The harness reads ./.harness.env from its working folder too."""
        with open(os.path.join(self.cwd, ".harness.env"), "w") as f:
            f.write("X=1\n")
        with self.assertRaises(ex.PreflightError):
            ex.preflight(self.cwd, self.harness)


class TestTicketFiles(unittest.TestCase):
    def test_hook_config_uses_absolute_interpreter_and_paths(self):
        """The hook must start from the same interpreter whatever PATH says, and find its
        mandate without any environment variable. A bare `python` would work on this machine
        and fail on the one with a different PATH, as a block that looks like a policy refusal."""
        with tempfile.TemporaryDirectory() as d:
            ex.write_ticket_files(d, os.path.join(d, "m.json"))
            with open(os.path.join(d, ".harness.hooks.json"), encoding="utf-8") as f:
                group = json.load(f)["hooks"]["PreToolUse"][0]
            handler = group["hooks"][0]
            self.assertTrue(os.path.isabs(handler["command"]))
            self.assertEqual(os.path.normcase(handler["command"]), os.path.normcase(sys.executable))
            self.assertTrue(all(os.path.isabs(a) for a in handler["args"] if a != "--mandate"))
            self.assertEqual(handler["onFailure"], "block")
            self.assertNotIn("matcher", group, "a matcher would skip other tools")


class RealProcess(unittest.TestCase):
    """Base: a private harness tree, a scripted model, one ticket folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tree = self._tmp.name
        self.harness = sup.copy_harness_tree(self.tree)
        self.cwd = os.path.join(self.tree, "ticket")
        os.makedirs(self.cwd)
        self.mandate = os.path.join(self.tree, "mandate.json")
        self.session = os.path.join(self.tree, "session.json")
        self.llm = None
        self.exec = None

    def tearDown(self):
        if self.exec is not None:
            self.exec.stop(force=True)
        if self.llm is not None:
            self.llm.close()
        self._tmp.cleanup()

    def boot(self, script, mandate=True, files=True):
        self.llm = sup.FakeLLM(script)
        if mandate:
            with open(self.mandate, "w", encoding="utf-8") as f:
                json.dump({"ticket_id": 1, "root": os.path.realpath(self.cwd)}, f)
        if files:
            ex.write_ticket_files(self.cwd, self.mandate)
        self.exec = ex.HarnessExecutor(cfg_for(self.llm), self.harness)
        self.exec.start(self.cwd, self.session)
        ev, _ = sup.wait_for(self.exec.events, "ready")
        return ev


class TestRealHarness(RealProcess):
    def test_env_file_injection_is_neutralised(self):
        """The repo-root .harness.env is loaded with setdefault AFTER the allow-list. A key,
        a debug switch and a skills path put there must not take effect, or 'controlled
        environment' is only a sentence."""
        with open(os.path.join(self.tree, ".harness.env"), "w", encoding="utf-8") as f:
            f.write("HARNESS_API_KEY=sk-canary\nHARNESS_DEBUG=1\nHARNESS_SKILLS_DIRS=C:/elsewhere\n"
                    "VLLM_API_KEY=sk-canary2\n")
        self.boot([{"text": "done"}])
        self.exec.send("hello")
        sup.wait_for(self.exec.events, "turn_end")
        self.assertTrue(self.llm.requests, "the scripted model was never asked")
        self.assertNotIn("authorization", self.llm.headers[0],
                         "an API key from .harness.env reached the model endpoint")
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "debug")),
                         "HARNESS_DEBUG from .harness.env took effect")

    def test_ready_in_safe_mode_and_no_stream(self):
        """The fixed settings are what the process really runs with, not just what we asked for."""
        ev = self.boot([{"text": "ok"}])
        self.assertEqual(ev["mode"], "safe")
        self.exec.send("hi")
        sup.wait_for(self.exec.events, "turn_end")
        self.assertFalse(self.llm.requests[0].get("stream"), "streaming was not switched off")

    def test_mandate_hook_really_runs(self):
        """If the hook never starts, every hook-policy test downstream is a false green. The
        model asks for a tool the mandate refuses; the refusal must carry our hook's own words,
        with no SUPERAGENT_* variable in the environment."""
        self.boot([{"calls": [("agent", {"task": "look around"})]}, {"text": "understood"}])
        self.exec.send("please delegate")
        res, _ = sup.wait_for(self.exec.events, "tool_result")
        self.assertIn("BLOCKED by hook", res["text"])
        self.assertIn("superagent mandate", res["text"])
        sup.wait_for(self.exec.events, "turn_end")

    def test_missing_mandate_blocks_everything(self):
        """A hook that cannot read its mandate must veto (and the config says onFailure=block);
        a silent pass-through would be an open door."""
        self.boot([{"calls": [("todo_write", {"todos": []})]}, {"text": "ok"}], mandate=False)
        self.exec.send("go")
        res, _ = sup.wait_for(self.exec.events, "tool_result")
        self.assertIn("BLOCKED by hook", res["text"])
        sup.wait_for(self.exec.events, "turn_end")

    def test_stop_leaves_no_process(self):
        """Stopping must end the whole tree, not just the python child (Windows terminate()
        leaves grandchildren)."""
        self.boot([{"text": "ok"}])
        pid = self.exec.pid
        self.exec.stop()
        self.assertFalse(self.exec.alive)
        self.assertIsNotNone(self.exec.proc.poll())
        self.assertGreater(pid, 0)


if __name__ == "__main__":
    unittest.main()
