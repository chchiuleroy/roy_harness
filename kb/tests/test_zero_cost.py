"""R2: not installing / installing-but-not-attaching kb leaves everything the probe observes unchanged (Gate 2 OPT2).
The probe records module set, subprocesses, sockets (bind, listen, connect), opened files, started threads, changed environment variables
and signal handlers, prompt and tool-schema hashes and the MCP config during the harness import (see tools/r2_probe.py); the two states must be
identical. This supports 'no observable side effect' for those observations; it does NOT measure CPU or memory cost, and timing is measured
separately (tools/measure_o2.py) because it cannot be asserted bit-for-bit. A probe that cannot tell two different harnesses apart proves
nothing, so it is also run against synthetic harnesses that DO react to kb."""
import json
import os
import subprocess
import sys
import unittest

from .support import SRC, tmpdir

ROOT = os.path.dirname(SRC)
PROBE = os.path.join(ROOT, "tools", "r2_probe.py")
HARNESS_DIR = os.environ.get("KB_HARNESS_DIR", "C:/Users/roy/roy_harness/src/harness")
IGNORED = {"import_s"}


def probe(harness_dir, workdir, kb_installed):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=SRC if kb_installed else "")
    r = subprocess.run([sys.executable, PROBE, harness_dir, workdir], capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise AssertionError(r.stderr[-600:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def differences(a, b):
    return sorted(k for k in a if k not in IGNORED and a[k] != b[k])


SYNTH_MCP = "class C:\n    servers = {}\n\n\ndef load(p):\n    return C()\n"


def synthetic(testcase, body):
    d = tmpdir(testcase)
    with open(os.path.join(d, "harness_vllm.py"), "w", encoding="utf-8", newline="") as f:
        f.write(body + "\n\ndef system_prompt():\n    return 'x'\n\n\ndef tools_for(depth):\n    return []\n")
    with open(os.path.join(d, "harness_mcp.py"), "w", encoding="utf-8", newline="") as f:
        f.write(SYNTH_MCP)
    return d


class TestKbIsAbsentFromTheTestEnvironment(unittest.TestCase):
    def test_kb_is_not_importable_without_the_path(self):
        """Guards the experiment: 'not installed' must really mean not importable."""
        r = subprocess.run([sys.executable, "-c", "import kb"], capture_output=True, env=dict(os.environ, PYTHONPATH=""))
        self.assertNotEqual(r.returncode, 0)


@unittest.skipUnless(os.path.isdir(HARNESS_DIR), "harness source not found (set KB_HARNESS_DIR)")
class TestRealHarness(unittest.TestCase):
    def test_installed_but_not_attached_changes_nothing_observable(self):
        wd = tmpdir(self)
        none, installed = probe(HARNESS_DIR, wd, False), probe(HARNESS_DIR, wd, True)
        self.assertEqual(differences(none, installed), [])
        self.assertEqual(none["popen"], [])               # importing the harness starts no process at all
        self.assertEqual((none["bind"], none["listen"]), ([], 0))
        self.assertNotIn("kb", none["modules"])
        self.assertNotIn("kb", installed["modules"])

    def test_same_with_an_existing_mcp_config(self):
        wd = tmpdir(self)
        with open(os.path.join(wd, ".harness.mcp.json"), "w", encoding="utf-8") as f:
            json.dump({"servers": {"other": {"type": "stdio", "command": "python", "args": ["x.py"]}}}, f)
        none, installed = probe(HARNESS_DIR, wd, False), probe(HARNESS_DIR, wd, True)
        self.assertEqual(differences(none, installed), [])
        self.assertEqual(none["mcp_servers"], ["other"])
        self.assertIsNotNone(none["mcp_config_sha256"])


class TestProbeCanSeeDifferences(unittest.TestCase):
    """Negative controls: harnesses that react to kb being installed must be reported as different."""

    def check(self, body, expected_key):
        d = synthetic(self, body)
        wd = tmpdir(self)
        diff = differences(probe(d, wd, False), probe(d, wd, True))
        self.assertIn(expected_key, diff)

    def test_extra_module_is_seen(self):
        self.check("try:\n    import kb\nexcept ImportError:\n    kb = None", "modules")

    def test_extra_subprocess_is_seen(self):
        self.check("import subprocess, sys\ntry:\n    import kb\n    subprocess.run([sys.executable, '-c', 'pass'])\nexcept ImportError:\n    pass", "popen")

    def test_extra_listening_socket_is_seen(self):
        self.check("import socket\ntry:\n    import kb\n    s = socket.socket()\n    s.bind(('127.0.0.1', 0))\n    s.listen()\nexcept ImportError:\n    pass", "bind")

    def test_extra_file_access_is_seen(self):
        self.check("try:\n    import kb\n    open(__file__, 'rb').close()\nexcept ImportError:\n    pass", "opens")

    def test_started_thread_is_seen(self):
        self.check("import threading, time\ntry:\n    import kb\n    threading.Thread(target=time.sleep, args=(0.2,), name='kb-worker').start()\nexcept ImportError:\n    pass", "new_threads")

    def test_changed_environment_is_seen(self):
        self.check("import os\ntry:\n    import kb\n    os.environ['KB_SIDE_EFFECT'] = '1'\nexcept ImportError:\n    pass", "env_changed")

    def test_outbound_connection_attempt_is_seen(self):
        self.check("import socket\ntry:\n    import kb\n    s = socket.socket()\n    s.settimeout(0.2)\n    s.connect_ex(('127.0.0.1', 9))\n    s.close()\nexcept ImportError:\n    pass", "connect")

    def test_changed_signal_handler_is_seen(self):
        self.check("import signal\ntry:\n    import kb\n    signal.signal(signal.SIGINT, lambda *a: None)\nexcept ImportError:\n    pass", "signal_handlers_changed")

    def test_changed_prompt_is_seen(self):
        d = synthetic(self, "try:\n    import kb\n    FLAG = 1\nexcept ImportError:\n    FLAG = 0")
        with open(os.path.join(d, "harness_vllm.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef system_prompt():\n    return 'x' + str(FLAG)\n")
        wd = tmpdir(self)
        self.assertIn("system_prompt_sha256", differences(probe(d, wd, False), probe(d, wd, True)))


if __name__ == "__main__":
    unittest.main()
