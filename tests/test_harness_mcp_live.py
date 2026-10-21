"""Live MCP tests against REAL FastMCP servers (not the hand-written fakes).

Skipped unless HARNESS_LIVE=1 and the interpreters below are set. Each interpreter must
belong to a venv that has fastmcp installed; two different generations are used so both
protocol eras meet a real server:

  HARNESS_MCP_LIVE_MODERN_PY   e.g. a venv with fastmcp 4.1.x (modern-only on stdio,
                               dual-era on HTTP)
  HARNESS_MCP_LIVE_LEGACY_PY   e.g. a venv with fastmcp 2.14.x (legacy on both)

What this adds over test_harness_mcp.py: the fakes were written by the same person who
wrote the client, so they can share one misreading of the spec. These servers cannot.
"""
import json
import os
import socket
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src", "harness"))
import harness_hooks  # noqa: E402
import harness_mcp  # noqa: E402
from test_harness_vllm import Base  # noqa: E402

LIVE = os.environ.get("HARNESS_LIVE") == "1"
MODERN_PY = os.environ.get("HARNESS_MCP_LIVE_MODERN_PY", "")
LEGACY_PY = os.environ.get("HARNESS_MCP_LIVE_LEGACY_PY", "")
SERVER = os.path.join(HERE, "live_fastmcp_server.py")
TOOLS = {"live__add", "live__shout", "live__fail"}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(LIVE and MODERN_PY and LEGACY_PY,
                     "set HARNESS_LIVE=1, HARNESS_MCP_LIVE_MODERN_PY and "
                     "HARNESS_MCP_LIVE_LEGACY_PY to run against real FastMCP servers")
class TestRealFastMcp(Base):
    def setUp(self):
        super().setUp()
        self.regs, self.procs = [], []

    def tearDown(self):
        for r in self.regs:
            r.close()
        for p in self.procs:
            harness_hooks.kill_tree(p)
        super().tearDown()

    def connect(self, spec):
        with open(".harness.mcp.json", "w", encoding="utf-8") as f:
            json.dump({"servers": {"live": {"timeout": 60, "trust": "always", **spec}}}, f)
        reg = harness_mcp.connect_all(harness_mcp.load(".harness.mcp.json"))
        self.regs.append(reg)
        self.assertEqual(reg.failures, {}, reg.failures)
        return reg

    def stdio(self, py):
        return self.connect({"type": "stdio", "command": py, "args": [SERVER, "stdio"]})

    def http(self, py):
        port = free_port()
        proc = subprocess.Popen([py, SERVER, "http", str(port)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        for _ in range(120):                                  # wait until it listens
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.25)
        return self.connect({"type": "http", "url": f"http://127.0.0.1:{port}/mcp"})

    def check_works(self, reg, era):
        server = reg.servers["live"]
        self.assertEqual(server.era, era, f"{server.era} {server.version}")
        self.assertEqual({t["name"] for t in reg.tools()}, TOOLS)
        self.assertEqual(reg.call("live__add", {"a": 2, "b": 3}), "5")
        self.assertEqual(reg.call("live__shout", {"text": "hi"}), "HI")
        failed = reg.call("live__fail", {"message": "kaboom"})
        self.assertTrue(failed.startswith("ERROR"), failed)
        for tool in reg.tools():
            self.assertEqual(tool["input_schema"]["type"], "object")

    def test_stdio_modern_only_server(self):
        """fastmcp 4.x on stdio answers server/discover and rejects initialize."""
        self.check_works(self.stdio(MODERN_PY), "modern")

    def test_stdio_legacy_server(self):
        """fastmcp 2.x answers server/discover with -32602 (not a modern error code), so
        the client must fall back to the initialize handshake."""
        reg = self.stdio(LEGACY_PY)
        self.check_works(reg, "legacy")
        self.assertIn(reg.servers["live"].version, harness_mcp.LEGACY_ACCEPTED)

    def test_http_server_that_speaks_both_eras_is_used_as_modern(self):
        self.check_works(self.http(MODERN_PY), "modern")

    def test_http_legacy_server_with_sessions_and_event_streams(self):
        """fastmcp 2.x over HTTP: a modern request gets 400 'Missing session ID', the
        handshake returns a session id, and replies arrive as an SSE stream."""
        reg = self.http(LEGACY_PY)
        self.check_works(reg, "legacy")
        self.assertTrue(reg.servers["live"].session)


@unittest.skipUnless(LIVE and MODERN_PY, "needs HARNESS_LIVE=1 and HARNESS_MCP_LIVE_MODERN_PY")
class TestRealModelUsesARealMcpTool(Base):
    """The whole chain: real vLLM model -> harness -> MCP stdio -> real FastMCP server."""

    def test_the_model_calls_add_and_reports_the_sum(self):
        with open(".harness.mcp.json", "w", encoding="utf-8") as f:
            json.dump({"servers": {"live": {"type": "stdio", "command": MODERN_PY,
                                            "args": [SERVER, "stdio"], "trust": "always",
                                            "timeout": 60}}}, f)
        env = dict(os.environ, PYTHONUTF8="1")
        p = subprocess.run(
            [sys.executable, "-X", "utf8",
             os.path.join(HERE, "..", "src", "harness", "harness_vllm.py"), "s.json"],
            input="Use the live__add tool to compute 17 + 25, then reply with just the number.\n",
            text=True, encoding="utf-8", capture_output=True, timeout=420, env=env)
        with open("s.json", encoding="utf-8") as f:
            messages = json.load(f)
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            self.skipTest("the model did not call the tool this time:\n" + p.stdout[-400:])
        self.assertIn("42", tool_msgs[0]["content"] + p.stdout)
        self.assertEqual(tool_msgs[0]["content"].strip(), "42")


if __name__ == "__main__":
    unittest.main()
