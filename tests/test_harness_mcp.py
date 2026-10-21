"""Tests for src/harness/harness_mcp.py (MCP client: stdio + Streamable HTTP, two eras).

Servers are fakes: tests/fake_mcp_server.py over stdio and _McpHttp below over HTTP,
each able to speak the modern (2026-07-28, stateless) or the legacy (initialize
handshake) dialect. Each test's docstring says WHY the behaviour matters.
"""
import base64
import http.server
import json
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src", "harness"))
import fake_mcp_server as fk  # noqa: E402
import harness_vllm as hv  # noqa: E402
from test_harness_vllm import Base, _LLMServer, llm_reply, tool_call_reply  # noqa: E402

FAKE = os.path.join(HERE, "fake_mcp_server.py")
ENV_KEYS = ("FAKE_MCP_ERA", "FAKE_MCP_LOG", "FAKE_MCP_LEGACY_REPLY_VERSION",
            "HT_MCP_SECRET_KEY", "HT_MCP_LISTED_KEY", "HT_MCP_TOKEN_VAR")
VALID_TOOLS = {"echo", "big", "image", "structured", "fail", "ask", "slow", "die",
               "envnames", "bad_name_", "a_b", "evil", "hdr"}


class McpBase(Base):
    def setUp(self):
        super().setUp()
        import harness_mcp as hm
        self.hm = hm
        self._env = {k: os.environ.get(k) for k in ENV_KEYS}
        self._mcp, self._probe = hv.MCP, hm.PROBE_TIMEOUT
        self.regs = []

    def tearDown(self):
        for r in self.regs:
            r.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        hv.MCP, self.hm.PROBE_TIMEOUT = self._mcp, self._probe
        super().tearDown()

    def era(self, era, **env):
        os.environ["FAKE_MCP_ERA"] = era
        os.environ["FAKE_MCP_LOG"] = os.path.abspath("mcp.log")
        for k, v in env.items():
            os.environ[k] = v

    def stdio(self, **extra):
        return {"type": "stdio", "command": sys.executable, "args": [FAKE],
                "timeout": 10, **extra}

    def write_cfg(self, servers):
        with open(".harness.mcp.json", "w", encoding="utf-8") as f:
            json.dump({"servers": servers}, f)
        return self.hm.load(".harness.mcp.json")

    def connect(self, servers):
        reg = self.hm.connect_all(self.write_cfg(servers))
        self.regs.append(reg)
        return reg

    def log(self):
        if not os.path.exists("mcp.log"):
            return []
        with open("mcp.log", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def methods(self):
        return [m.get("method") for m in self.log()]


# ------------------------------------------------------------------- config
class TestMcpConfig(McpBase):
    def test_missing_file_means_no_servers(self):
        cfg = self.hm.load("nope.json")
        self.assertTrue(cfg.is_empty())
        reg = self.hm.connect_all(cfg)
        self.assertEqual((reg.tools(), reg.failures), ([], {}))

    def test_a_bad_config_fails_loudly(self):
        """A typo must not silently drop a server or widen its trust."""
        ok = {"type": "stdio", "command": "python"}
        cases = {
            "not json": "{nope",
            "no servers key": "{}",
            "extra root key": json.dumps({"servers": {"a": ok}, "serverz": {}}),
            "unknown server key": json.dumps({"servers": {"a": {**ok, "comand": "x"}}}),
            "unknown type": json.dumps({"servers": {"a": {"type": "pipe", "command": "x"}}}),
            "stdio without command": json.dumps({"servers": {"a": {"type": "stdio"}}}),
            "shell-form command": json.dumps({"servers": {"a": {"type": "stdio",
                                                                "command": "python s.py"}}}),
            "stdio with url": json.dumps({"servers": {"a": {**ok, "url": "https://x/mcp"}}}),
            "http without url": json.dumps({"servers": {"a": {"type": "http"}}}),
            "http with command": json.dumps({"servers": {"a": {"type": "http",
                                                               "url": "https://x/mcp",
                                                               "command": "x"}}}),
            "remote http without opt-in": json.dumps({"servers": {"a": {
                "type": "http", "url": "http://mcp.corp/mcp"}}}),
            "bad server name": json.dumps({"servers": {"my server": ok}}),
            "underscore in server name": json.dumps({"servers": {"my_server": ok}}),
            "bad trust": json.dumps({"servers": {"a": {**ok, "trust": "yes"}}}),
            "env not a list": json.dumps({"servers": {"a": {**ok, "env": "X"}}}),
            "duplicate server": '{"servers": {"a": {"type": "stdio", "command": "p"}, '
                                '"a": {"type": "stdio", "command": "q"}}}',
        }
        for t in ("Infinity", "0", "-1", "601"):
            cases[f"timeout {t}"] = ('{"servers": {"a": {"type": "stdio", "command": "p", '
                                     '"timeout": %s}}}' % t)
        for label, text in cases.items():
            with open(".harness.mcp.json", "w", encoding="utf-8") as f:
                f.write(text)
            with self.assertRaises(self.hm.McpConfigError, msg=label):
                self.hm.load(".harness.mcp.json")

    def test_internal_http_needs_the_explicit_switch_at_load_time(self):
        cfg = self.write_cfg({"corp": {"type": "http", "url": "http://mcp.corp/mcp",
                                       "allow_http": True}})
        self.assertFalse(cfg.is_empty())
        self.write_cfg({"loop": {"type": "http", "url": "http://127.0.0.1:1/mcp"}})
        self.write_cfg({"tls": {"type": "http", "url": "https://mcp.corp/mcp"}})


# -------------------------------------------------------------------- stdio
class TestMcpStdio(McpBase):
    def test_both_eras_connect_list_every_page_and_remember_the_era(self):
        """Eras differ on the wire; the model must see the same tools either way."""
        for era, expected in (("modern", "modern"), ("legacy", "legacy"),
                              ("dual", "modern"), ("dual_rejects_modern", "legacy")):
            self.era(era)
            reg = self.connect({"fake": self.stdio()})
            self.assertEqual(reg.failures, {}, era)
            self.assertEqual(reg.servers["fake"].era, expected, era)
            names = {t["name"] for t in reg.tools()}
            self.assertEqual(names, {f"fake__{n}" for n in VALID_TOOLS}, era)   # both pages
            reg.close()

    def test_a_modern_server_never_gets_a_legacy_handshake(self):
        self.era("modern")
        self.connect({"fake": self.stdio()})
        self.assertNotIn("initialize", self.methods())
        self.assertIn("server/discover", self.methods())

    def test_modern_requests_carry_version_identity_and_capabilities(self):
        """Stateless protocol: every request must say what it speaks."""
        self.era("modern")
        reg = self.connect({"fake": self.stdio()})
        reg.call("fake__echo", {"text": "hi"})
        reqs = [m for m in self.log() if m.get("method") in ("tools/list", "tools/call")]
        self.assertTrue(reqs)
        for m in reqs:
            meta = m["params"]["_meta"]
            self.assertEqual(meta["io.modelcontextprotocol/protocolVersion"], "2026-07-28")
            self.assertIn("name", meta["io.modelcontextprotocol/clientInfo"])
            self.assertEqual(meta["io.modelcontextprotocol/clientCapabilities"], {})

    def test_only_a_modern_error_with_a_legacy_list_leads_to_the_handshake(self):
        """A modern 'unsupported version' error naming 2025-11-25 means: use that
        version, via initialize (spec: do not fall back blindly)."""
        self.era("dual_rejects_modern")
        self.connect({"fake": self.stdio()})
        inits = [m for m in self.log() if m.get("method") == "initialize"]
        self.assertEqual(len(inits), 1)
        self.assertEqual(inits[0]["params"]["protocolVersion"], "2025-11-25")
        self.assertIn("notifications/initialized", self.methods())
        self.assertLess(self.methods().index("initialize"), self.methods().index("tools/list"))

    def test_legacy_reply_versions_are_checked(self):
        """A server answering with a version we do not speak must be refused, not used."""
        self.era("legacy", FAKE_MCP_LEGACY_REPLY_VERSION="1999-01-01")
        reg = self.connect({"fake": self.stdio()})
        self.assertIn("fake", reg.failures)
        self.era("legacy", FAKE_MCP_LEGACY_REPLY_VERSION="2025-06-18")
        reg = self.connect({"fake": self.stdio()})
        self.assertEqual(reg.failures, {})
        self.assertTrue(any("2025-06-18" in w for w in reg.warnings), reg.warnings)

    def test_tool_names_descriptions_and_bad_definitions_are_sanitised(self):
        """Tool metadata comes from a stranger and goes straight into the model's
        context: it is an injection channel (book ch 11)."""
        self.era("modern")
        reg = self.connect({"fake": self.stdio()})
        tools = {t["name"]: t for t in reg.tools()}
        self.assertIn("fake__bad_name_", tools)               # space and ! replaced
        self.assertIn("fake__a_b", tools)                      # first of the collision
        self.assertEqual(sum(n.startswith("fake__a_b") for n in tools), 1)
        self.assertNotIn("fake__broken", tools)                # invalid schema skipped
        self.assertTrue(any("a_b" in w for w in reg.warnings))
        self.assertTrue(any("broken" in w for w in reg.warnings))
        desc = tools["fake__evil"]["description"]
        self.assertTrue(desc.startswith("[mcp:fake]"))
        self.assertNotIn("\x07", desc)
        self.assertNotIn("\x1b", desc)
        self.assertLessEqual(len(desc), 1100)
        for t in tools.values():
            self.assertEqual(t["input_schema"]["type"], "object")

    def test_call_results_become_plain_text_for_every_content_type(self):
        self.era("modern")
        reg = self.connect({"fake": self.stdio()})
        self.assertEqual(reg.call("fake__echo", {"text": "hi"}), "echo:hi")
        img = reg.call("fake__image", {})
        self.assertIn("image omitted", img)
        self.assertNotIn("QUJD", img)                         # binary never enters the array
        self.assertIn("file:///a.txt", img)
        self.assertIn("embedded text", img)
        self.assertIn("42", reg.call("fake__structured", {}))
        failed = reg.call("fake__fail", {})
        self.assertTrue(failed.startswith("ERROR"), failed)
        self.assertIn("boom", failed)
        asked = reg.call("fake__ask", {})
        self.assertTrue(asked.startswith("ERROR"), asked)
        self.assertIn("not supported", asked)
        self.assertTrue(reg.call("nope__x", {}).startswith("ERROR"))
        self.assertTrue(reg.call("fake__zzz", {}).startswith("ERROR"))

    def test_a_slow_tool_times_out_instead_of_hanging_the_loop(self):
        self.era("modern")
        reg = self.connect({"fake": self.stdio(timeout=1)})
        t0 = time.time()
        out = reg.call("fake__slow", {})
        self.assertLess(time.time() - t0, 4)
        self.assertTrue(out.startswith("ERROR"), out)
        self.assertIn("timed out", out)

    def test_a_crashed_server_is_restarted_once(self):
        """Spec: a client should restart a server that dies; the in-flight call is lost."""
        self.era("modern")
        reg = self.connect({"fake": self.stdio()})
        self.assertTrue(reg.call("fake__die", {}).startswith("ERROR"))
        self.assertEqual(reg.call("fake__echo", {"text": "again"}), "echo:again")
        self.assertGreaterEqual(self.methods().count("server/discover"), 2)

    def test_the_server_environment_is_scrubbed_but_lists_are_honoured(self):
        os.environ["HT_MCP_SECRET_KEY"] = "s"
        os.environ["HT_MCP_LISTED_KEY"] = "l"
        self.era("modern")
        plain = self.connect({"fake": self.stdio()}).call("fake__envnames", {})
        listed = self.connect({"fake": self.stdio(env=["HT_MCP_LISTED_KEY"])}).call(
            "fake__envnames", {})
        self.assertNotIn("HT_MCP_SECRET_KEY", plain)
        self.assertNotIn("HT_MCP_LISTED_KEY", plain)
        self.assertIn("HT_MCP_LISTED_KEY", listed)
        self.assertNotIn("HT_MCP_SECRET_KEY", listed)

    def test_one_broken_server_does_not_stop_the_others(self):
        self.era("modern")
        good = self.stdio()
        reg = self.connect({"good": good, "gone": {"type": "stdio",
                                                   "command": "no-such-program-xyz"}})
        self.assertIn("gone", reg.failures)
        self.assertEqual(reg.call("good__echo", {"text": "x"}), "echo:x")

    def test_a_server_that_exits_or_never_answers_fails_fast(self):
        self.hm.PROBE_TIMEOUT = 1
        for era in ("dead", "silent"):
            self.era(era)
            t0 = time.time()
            reg = self.connect({"fake": self.stdio(timeout=2)})
            self.assertIn("fake", reg.failures, era)
            self.assertLess(time.time() - t0, 8, era)

    def test_close_leaves_no_process_behind_and_is_idempotent(self):
        self.era("modern")
        reg = self.connect({"fake": self.stdio()})
        proc = reg.servers["fake"].proc
        reg.close()
        reg.close()
        self.assertIsNotNone(proc.poll())


# --------------------------------------------------------------------- http
def b64(value):
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


class _Target(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        type(self).seen.append((self.command, self.path, self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    do_GET = do_POST          # urllib turns a redirected POST into a GET: record that too


class _McpHttp(http.server.BaseHTTPRequestHandler):
    mode, sse, big, redirect_to, supported = "modern", False, False, None, None
    plain_errors = False
    seen = []

    def log_message(self, *a):
        pass

    def send_json(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def err(self, code, rpc_code, message, data=None, id_=None):
        e = {"code": rpc_code, "message": message}
        if data is not None:
            e["data"] = data
        self.send_json(code, {"jsonrpc": "2.0", "id": id_, "error": e})

    def finish_result(self, id_, result):
        if not type(self).sse:
            return self.send_json(200, {"jsonrpc": "2.0", "id": id_, "result": result})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        progress = {"jsonrpc": "2.0", "method": "notifications/progress",
                    "params": {"progress": 1}}
        for ev in (progress, {"jsonrpc": "2.0", "id": id_, "result": result}):
            self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
            self.wfile.flush()

    def do_POST(self):
        cls = type(self)
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        headers = {k.lower(): v for k, v in self.headers.items()}
        cls.seen.append({"headers": headers, "body": body})
        if cls.redirect_to:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{cls.redirect_to}/x")
            self.end_headers()
            return
        if cls.big:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b" " * 2_000_000 + b"{}")
            return
        method, id_, params = body.get("method"), body.get("id"), body.get("params") or {}
        if cls.mode == "legacy":
            return self.legacy(method, id_, params, headers)
        return self.modern(method, id_, params, headers)

    def tools(self, params, modern):
        page = fk.PAGE2 if params.get("cursor") == "p2" else fk.PAGE1
        extra = ([{"name": "badhdr", "description": "bad x-mcp-header",
                   "inputSchema": {"type": "object", "properties": {
                       "n": {"type": "number", "x-mcp-header": "N"}}}}]
                 if modern and params.get("cursor") == "p2" else [])
        result = {"tools": page + extra}
        if params.get("cursor") != "p2":
            result["nextCursor"] = "p2"
        return result

    def call(self, id_, params, modern):
        result = fk.call_tool(params.get("name"), params.get("arguments") or {})
        if result is None:
            return self.err(200, -32602, "Unknown tool", id_=id_)
        if modern:
            result = {"resultType": "complete", **result}
        self.finish_result(id_, result)

    def modern(self, method, id_, params, headers):
        meta = (params.get("_meta") or {})
        version = meta.get("io.modelcontextprotocol/protocolVersion")
        sup = type(self).supported
        if sup is not None and "2026-07-28" not in sup:
            return self.err(400, -32022, "Unsupported protocol version",
                            {"supported": sup, "requested": version}, id_)
        if headers.get("mcp-protocol-version") != "2026-07-28" or version != "2026-07-28" \
                or headers.get("mcp-method") != method:
            return self.err(400, -32020, "Header mismatch", id_=id_)
        if method == "server/discover":
            return self.finish_result(id_, {"resultType": "complete",
                                            "supportedVersions": ["2026-07-28"],
                                            "capabilities": {"tools": {}}})
        if method == "tools/list":
            return self.finish_result(id_, {"resultType": "complete",
                                            **self.tools(params, True)})
        if method == "tools/call":
            name = params.get("name")
            if headers.get("mcp-name") not in (name, b64(name)):
                return self.err(400, -32020, "Mcp-Name mismatch", id_=id_)
            if name == "hdr":
                region = (params.get("arguments") or {}).get("region")
                expected = region if region and region.isascii() and region.strip() == region \
                    else b64(region or "")
                if headers.get("mcp-param-region") != expected:
                    return self.err(400, -32020, "Mcp-Param mismatch", id_=id_)
            return self.call(id_, params, True)
        return self.err(404, -32601, "Method not found", id_=id_)

    def legacy(self, method, id_, params, headers):
        if method == "initialize":
            return self.send_json(200, {"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": params.get("protocolVersion"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}},
                {"Mcp-Session-Id": "sess-1"})
        if headers.get("mcp-session-id") != "sess-1":
            if type(self).plain_errors:                      # an old server: no JSON body
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            return self.err(400, -32600, "Missing or bad session", id_=id_)
        if id_ is None:                                       # notification
            self.send_response(202)
            self.end_headers()
            return
        if method == "tools/list":
            return self.finish_result(id_, self.tools(params, False))
        if method == "tools/call":
            return self.call(id_, params, False)
        return self.err(404, -32601, "Method not found", id_=id_)


class TestMcpHttp(McpBase):
    def setUp(self):
        super().setUp()
        c = _McpHttp
        c.mode, c.sse, c.big, c.redirect_to, c.supported, c.seen = \
            "modern", False, False, None, None, []
        c.plain_errors = False
        _Target.seen = []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _McpHttp)
        self.tgt = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Target)
        for s in (self.srv, self.tgt):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/mcp"

    def tearDown(self):
        for s in (self.srv, self.tgt):
            s.shutdown()
        super().tearDown()

    def http(self, **extra):
        return self.connect({"web": {"type": "http", "url": self.url, "timeout": 10, **extra}})

    def methods_seen(self):
        return [r["body"].get("method") for r in _McpHttp.seen]

    def test_modern_http_needs_no_handshake_and_sends_the_required_headers(self):
        reg = self.http()
        self.assertEqual(reg.failures, {})
        self.assertEqual(reg.servers["web"].era, "modern")
        self.assertNotIn("initialize", self.methods_seen())
        self.assertEqual(reg.call("web__echo", {"text": "hi"}), "echo:hi")
        call = [r for r in _McpHttp.seen if r["body"].get("method") == "tools/call"][0]
        self.assertEqual(call["headers"]["mcp-protocol-version"], "2026-07-28")
        self.assertEqual(call["headers"]["mcp-method"], "tools/call")
        self.assertEqual(call["headers"]["mcp-name"], "echo")
        self.assertIn("text/event-stream", call["headers"]["accept"])
        self.assertIn("application/json", call["headers"]["accept"])

    def test_legacy_http_falls_back_to_initialize_and_echoes_the_session(self):
        _McpHttp.mode = "legacy"
        reg = self.http()
        self.assertEqual(reg.failures, {})
        self.assertEqual(reg.servers["web"].era, "legacy")
        self.assertEqual(reg.call("web__echo", {"text": "hi"}), "echo:hi")
        methods = self.methods_seen()
        self.assertIn("initialize", methods)
        self.assertIn("notifications/initialized", methods)
        later = [r for r in _McpHttp.seen
                 if r["body"].get("method") in ("tools/list", "tools/call",
                                                "notifications/initialized")]
        for r in later:
            self.assertEqual(r["headers"].get("mcp-session-id"), "sess-1")
            self.assertEqual(r["headers"].get("mcp-protocol-version"), "2025-11-25")

    def test_legacy_http_with_empty_error_bodies_still_falls_back(self):
        """Old servers answer an unknown request with a bare 404 and no JSON; that is
        'not a recognised modern error', so the client must try the handshake."""
        _McpHttp.mode, _McpHttp.plain_errors = "legacy", True
        reg = self.http()
        self.assertEqual(reg.failures, {})
        self.assertEqual(reg.servers["web"].era, "legacy")

    def test_a_modern_error_does_not_trigger_the_legacy_handshake(self):
        """Spec: a 400 whose body is a recognised modern error means 'modern server';
        falling back to initialize there would talk legacy to a modern peer."""
        _McpHttp.supported = ["2027-01-01"]
        reg = self.http()
        self.assertIn("web", reg.failures)
        self.assertNotIn("initialize", self.methods_seen())

    def test_sse_responses_are_parsed_and_notifications_ignored(self):
        _McpHttp.sse = True
        reg = self.http()
        self.assertEqual(reg.failures, {})
        self.assertEqual(reg.call("web__echo", {"text": "sse"}), "echo:sse")

    def test_x_mcp_header_parameters_are_mirrored_into_headers(self):
        """Spec: a modern HTTP client MUST mirror them; the server rejects otherwise."""
        reg = self.http()
        self.assertEqual(reg.call("web__hdr", {"region": "us-west1", "query": "q"}), "hdr-ok")
        self.assertEqual(reg.call("web__hdr", {"region": "中文", "query": "q"}), "hdr-ok")
        self.assertEqual(reg.call("web__hdr", {"region": " padded ", "query": "q"}), "hdr-ok")
        calls = [r for r in _McpHttp.seen if r["body"].get("method") == "tools/call"]
        self.assertEqual(calls[0]["headers"]["mcp-param-region"], "us-west1")
        self.assertEqual(calls[1]["headers"]["mcp-param-region"], b64("中文"))

    def test_invalid_x_mcp_header_definitions_exclude_only_that_tool(self):
        reg = self.http()
        names = {t["name"] for t in reg.tools()}
        self.assertNotIn("web__badhdr", names)
        self.assertIn("web__echo", names)
        self.assertTrue(any("badhdr" in w for w in reg.warnings), reg.warnings)

    def test_a_redirect_is_a_failure_and_credentials_go_nowhere_else(self):
        os.environ["HT_MCP_TOKEN_VAR"] = "tok"
        _McpHttp.redirect_to = self.tgt.server_address[1]
        reg = self.http(headers_env={"Authorization": "HT_MCP_TOKEN_VAR"})
        self.assertIn("web", reg.failures)
        self.assertEqual(_Target.seen, [])

    def test_the_token_comes_from_the_environment_and_a_missing_one_sends_nothing(self):
        os.environ["HT_MCP_TOKEN_VAR"] = "tok-1"
        reg = self.http(headers_env={"Authorization": "HT_MCP_TOKEN_VAR"})
        self.assertEqual(reg.failures, {})
        self.assertTrue(all(r["headers"].get("authorization") == "tok-1"
                            for r in _McpHttp.seen))
        reg.close()
        os.environ.pop("HT_MCP_TOKEN_VAR")
        _McpHttp.seen = []
        reg = self.http(headers_env={"Authorization": "HT_MCP_TOKEN_VAR"})
        self.assertIn("web", reg.failures)
        self.assertEqual(_McpHttp.seen, [])

    def test_an_oversized_response_is_a_failure_not_a_memory_problem(self):
        _McpHttp.big = True
        reg = self.http()
        self.assertIn("web", reg.failures)
        self.assertIn("large", reg.failures["web"])

    def test_http_errors_on_a_call_become_error_text(self):
        reg = self.http()
        self.assertTrue(reg.call("web__nonesuch", {}).startswith("ERROR"))
        _McpHttp.supported = ["2027-01-01"]
        self.assertTrue(reg.call("web__echo", {"text": "x"}).startswith("ERROR"))


# ------------------------------------------------------- inside the harness
class TestMcpInHarness(McpBase):
    def setUp(self):
        super().setUp()
        self.asked, self.answer = [], "n"
        hv.read_line = lambda prompt: (self.asked.append(prompt), self.answer)[1]
        self.era("modern")

    def install(self, **extra):
        hv.MCP = self.connect({"fake": self.stdio(**extra)})

    def test_mcp_tools_reach_the_main_agent_only(self):
        self.install()
        names = {t["name"] for t in hv.tools_for(0)}
        self.assertIn("fake__echo", names)
        self.assertIn("write_file", names)
        for depth in (1, 2):
            self.assertFalse(any(n.startswith("fake__") for n in
                                 {t["name"] for t in hv.tools_for(depth)}), depth)

    def test_a_subagent_cannot_call_an_mcp_tool_even_by_name(self):
        """The tool list hides them, but a model can still name one."""
        self.install(trust="always")
        out = hv.execute_tool("fake__echo", {"text": "x"}, depth=1)
        self.assertIn("not available to subagents", out)
        self.assertNotIn("tools/call", self.methods())

    def test_every_mcp_call_asks_the_human_even_in_auto_mode(self):
        """A stranger's tool runs with the harness's trust: the classifier is not a
        substitute for a person (default; `trust: always` is the explicit exception)."""
        self.install()
        for mode in ("normal", "safe", "auto"):
            hv.MODE = mode
            self.asked.clear()
            self.answer = "n"
            out = hv.execute_tool("fake__echo", {"text": "hi"})
            self.assertIn("DENIED", out, mode)
            self.assertEqual(len(self.asked), 1, mode)
            self.assertIn("fake", self.asked[0])
            self.assertIn("echo", self.asked[0])
            self.assertNotIn("always", self.asked[0].lower())
        self.assertNotIn("tools/call", self.methods())          # denied = never sent
        self.answer = "y"
        self.assertEqual(hv.execute_tool("fake__echo", {"text": "hi"}), "echo:hi")

    def test_trust_always_skips_the_question_for_that_server_only(self):
        hv.MCP = self.connect({"fake": self.stdio(trust="always"),
                               "other": self.stdio()})
        self.assertEqual(hv.execute_tool("fake__echo", {"text": "a"}), "echo:a")
        self.assertEqual(self.asked, [])
        self.assertIn("DENIED", hv.execute_tool("other__echo", {"text": "a"}))

    def test_a_pre_hook_can_veto_an_mcp_tool(self):
        import harness_hooks as hh
        self.install(trust="always")
        with open("deny.py", "w", encoding="utf-8") as f:
            f.write("import sys\nsys.stderr.write('policy: no')\nsys.exit(2)")
        with open(".harness.hooks.json", "w", encoding="utf-8") as f:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "fake__.*", "hooks": [
                {"type": "command", "command": sys.executable,
                 "args": [os.path.abspath("deny.py")]}]}]}}, f)
        old = hv.HOOKS
        hv.HOOKS = hh.load(".harness.hooks.json")
        try:
            out = hv.execute_tool("fake__echo", {"text": "hi"})
        finally:
            hv.HOOKS = old
        self.assertIn("BLOCKED by hook", out)
        self.assertNotIn("tools/call", self.methods())

    def test_results_are_truncated_like_every_other_tool(self):
        self.install(trust="always")
        out = hv.execute_tool("fake__big", {})
        self.assertLess(len(out), hv.MAX_RESULT_CHARS + 500)
        self.assertIn("truncated", out)

    def test_the_model_cannot_edit_the_mcp_config_or_the_server_script(self):
        """Otherwise it could add a server (or rewrite one) and have it run. The script
        sits INSIDE the project root here: outside it the write-confinement rule would
        refuse anyway and the test would pass for the wrong reason."""
        import shutil
        shutil.copy(FAKE, "srv.py")
        hv.MCP = self.connect({"fake": self.stdio(args=[os.path.abspath("srv.py")])})
        for path in (".harness.mcp.json", "./.harness.mcp.json", ".HARNESS.MCP.JSON",
                     "srv.py", "./srv.py", "SRV.PY", os.path.abspath("srv.py")):
            for tool in ("write_file", "edit_file"):
                args = {"path": path, "content": "{}", "old_text": "a", "new_text": "b"}
                out = hv.gate(tool, args)
                self.assertIn("protected", (out or "").lower(), (tool, path, out))
        for cmd in ("echo {} > .harness.mcp.json", "echo x > srv.py",
                    "echo x > .harness^.mcp.json", "echo x > SRV^.PY"):
            self.assertIn("BLOCKED", hv.gate("run_command", {"command": cmd}) or "", cmd)
        self.assertIsNone(hv.gate("run_command", {"command": "echo x > other_srv.py2"}))

    def test_the_system_prompt_warns_that_mcp_output_is_untrusted(self):
        self.install()
        self.assertIn("untrusted", hv.system_prompt().lower())
        hv.MCP = self.hm.EMPTY
        self.assertNotIn("MCP", hv.system_prompt())

    def test_slash_mcp_lists_servers_era_and_tool_count(self):
        import contextlib
        import io
        self.install()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hv.slash("/mcp", [])
        text = buf.getvalue()
        self.assertIn("fake", text)
        self.assertIn("modern", text)
        self.assertIn(str(len(VALID_TOOLS)), text)


class TestMcpEndToEnd(McpBase):
    """main() as a real process against a scripted model and the fake MCP server."""

    def setUp(self):
        super().setUp()
        _LLMServer.script, _LLMServer.bodies = [], []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LLMServer)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.era("modern")

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def run_harness(self, stdin_text):
        import subprocess
        env = {k: v for k, v in os.environ.items() if not k.startswith(("VLLM_", "HARNESS_"))}
        env.update(HARNESS_BASE_URL=f"http://127.0.0.1:{self.srv.server_address[1]}/v1",
                   HARNESS_STREAM="0", PYTHONUTF8="1")
        return subprocess.run([sys.executable, "-X", "utf8",
                               os.path.join(HERE, "..", "src", "harness", "harness_vllm.py"),
                               "session.json"], input=stdin_text, capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=120)

    def test_a_tool_call_travels_through_the_real_loop_and_a_broken_server_is_skipped(self):
        with open(".harness.mcp.json", "w", encoding="utf-8") as f:
            json.dump({"servers": {
                "fake": {"type": "stdio", "command": sys.executable, "args": [FAKE],
                         "trust": "always", "timeout": 10},
                "gone": {"type": "stdio", "command": "no-such-program-xyz"}}}, f)
        _LLMServer.script = [tool_call_reply("fake__echo", {"text": "via-loop"}),
                             llm_reply("done")]
        out = self.run_harness("use the tool\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        first = _LLMServer.bodies[0]
        self.assertIn("fake__echo", [t["function"]["name"] for t in first["tools"]])
        self.assertIn("untrusted", first["messages"][0]["content"].lower())
        tool_msgs = [m for m in _LLMServer.bodies[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(tool_msgs[0]["content"], "echo:via-loop")
        self.assertIn("gone", out.stdout)                    # the failure was reported

    def test_a_broken_mcp_config_stops_the_harness_before_any_model_call(self):
        with open(".harness.mcp.json", "w", encoding="utf-8") as f:
            f.write('{"servers": {"a": {"type": "pipe"}}}')
        _LLMServer.script = [llm_reply("nope")]
        out = self.run_harness("hello\n")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("mcp config error", out.stderr)
        self.assertEqual(_LLMServer.bodies, [])


if __name__ == "__main__":
    unittest.main()
