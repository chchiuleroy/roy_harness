"""Tests for src/harness/harness_gui.py and the harness' --jsonl mode.

The GUI can make the harness run commands, so half of these are about who is allowed to ask
(token, Host, Origin, content type). The other half run real harness processes against a
scripted fake model: events come out as JSON lines, an approval is answered by its id and by
nothing else, and a stopped session resumes from its file.

Run:  python -X utf8 -m unittest discover -s tests -v
Each test's docstring says WHY the behaviour matters, not just what it is.
"""
import http.client
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src", "harness")
sys.path.insert(0, SRC)
import harness_gui as gui  # noqa: E402

TOKEN = "test-token-123"


class FakeLLM(http.server.BaseHTTPRequestHandler):
    """Scripted OpenAI-style server: each request pops the next reply (JSON, or ("sse", text))."""
    script = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        item = type(self).script.pop(0)
        if isinstance(item, tuple):                          # streaming reply
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            text = item[1]
            evs = [{"choices": [{"index": 0, "delta": {"content": text[:3]}}]},
                   {"choices": [{"index": 0, "delta": {"content": text[3:]}}]},
                   {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                   {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}]
            for ev in evs:
                self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            return
        body = json.dumps(item).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def text_reply(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}


def command_reply(command):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]}}],
        "usage": {}}


class GuiBase(unittest.TestCase):
    stream = "0"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = os.path.join(self._tmp.name, "project")
        os.mkdir(self.project)
        self.home = os.path.join(self._tmp.name, "home")
        self._patches = [
            mock.patch.object(gui, "HOME", self.home),
            mock.patch.object(gui, "SESSIONS", os.path.join(self.home, "sessions")),
            mock.patch.object(gui, "REGISTRY", gui.Registry()),
        ]
        for p in self._patches:
            p.start()
        FakeLLM.script = []
        self.llm = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=self.llm.serve_forever, daemon=True).start()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("VLLM_", "HARNESS_"))}
        env.update(HARNESS_BASE_URL=f"http://127.0.0.1:{self.llm.server_address[1]}/v1",
                   HARNESS_STREAM=self.stream, HARNESS_MODEL="fake", PYTHONUTF8="1")
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.srv = gui.make_server(0, TOKEN, self.project)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        gui.REGISTRY.stop_all()
        self.srv.shutdown()
        self.srv.server_close()
        self.llm.shutdown()
        self._env.stop()
        for p in self._patches:
            p.stop()
        for _ in range(20):                                  # a child may still hold the folder
            try:
                self._tmp.cleanup()
                break
            except OSError:
                time.sleep(0.2)

    def call(self, method, path, body=None, headers=None, raw=None):
        h = {"Host": f"127.0.0.1:{self.port}", "Authorization": f"Bearer {TOKEN}",
             "Content-Type": "application/json"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request(method, path, body=raw if raw is not None else
                  (json.dumps(body) if body is not None else None), headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    def new_session(self, mode="normal"):
        st, s = self.call("POST", "/api/sessions", {"cwd": self.project, "mode": mode})
        self.assertEqual(st, 200, s)
        return s["id"], gui.REGISTRY.get(s["id"])

    def wait(self, session, pred, what, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            with session.cond:
                for ev in session.events:
                    if pred(ev):
                        return ev
            time.sleep(0.05)
        self.fail(f"no event: {what}; saw {[e.get('kind') for e in session.events]} "
                  f"{[e.get('text') for e in session.events if e.get('kind') in ('stderr', 'log')]}")

    def kinds(self, session):
        return [e["kind"] for e in session.events]


class TestWhoMayAsk(GuiBase):
    """The server can start commands on this machine: every way in has to be shut."""

    def test_no_token_no_api(self):
        """A page in another tab of your browser can reach localhost; without the token it gets nothing."""
        for headers in ({"Authorization": None}, {"Authorization": "Bearer wrong"},
                        {"Authorization": "Basic " + TOKEN}):
            st, _ = self.call("GET", "/api/sessions", headers=headers)
            self.assertEqual(st, 401, headers)

    def test_a_foreign_host_name_is_refused(self):
        """DNS rebinding: evil.example resolving to 127.0.0.1 would carry its own Host header."""
        st, _ = self.call("GET", "/api/sessions", headers={"Host": "evil.example"})
        self.assertEqual(st, 403)
        st, _ = self.call("GET", "/", headers={"Host": "evil.example"})
        self.assertEqual(st, 403)

    def test_a_foreign_origin_is_refused(self):
        """A cross-site POST carries Origin; even with a leaked token it must not be accepted."""
        st, _ = self.call("POST", "/api/sessions", {"cwd": self.project},
                          headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, _ = self.call("GET", "/api/sessions", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(st, 200)

    def test_posts_must_be_json(self):
        """A form post (text/plain) skips the browser's preflight; requiring JSON restores it."""
        st, _ = self.call("POST", "/api/sessions", raw="{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)

    def test_bad_bodies_and_folders(self):
        st, _ = self.call("POST", "/api/sessions", raw="not json")
        self.assertEqual(st, 400)
        st, _ = self.call("POST", "/api/sessions", {"cwd": os.path.join(self.project, "nope")})
        self.assertEqual(st, 400)
        st, _ = self.call("POST", "/api/sessions", {"cwd": self.project, "mode": "yolo"})
        self.assertEqual(st, 400)
        st, _ = self.call("POST", "/api/sessions/../../x/send", {"text": "x"})
        self.assertEqual(st, 404)

    def test_the_page_carries_a_nonce_csp_and_no_token(self):
        """The page is the one place model text meets the DOM: a CSP with a nonce backs up
        textContent, and the token must never be in what the server sends."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        page = r.read().decode("utf-8")
        csp = r.getheader("Content-Security-Policy")
        self.assertEqual(r.status, 200)
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn(f'nonce="{nonce}"', page)
        self.assertNotIn("__NONCE__", page)
        self.assertNotIn(TOKEN, page)
        self.assertNotIn("unsafe-inline", csp)
        self.assertFalse("innerHTML" in page or "insertAdjacentHTML" in page   # the rule the page lives by
                         or "outerHTML" in page or "document.write" in page)


class TestMessagesToEvents(unittest.TestCase):
    def test_a_session_file_becomes_the_events_a_live_run_makes(self):
        """Resume shows the old conversation with the same renderer as a live one."""
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "ok", "tool_calls": [
                    {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a"}'}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "data"}]
        ev = gui.messages_to_events(msgs)
        self.assertEqual([e["kind"] for e in ev], ["user", "assistant", "tool_call", "tool_result"])
        self.assertEqual(ev[2]["id"], ev[3]["id"])
        self.assertTrue(all(e["history"] for e in ev))


class TestSessionBuffer(unittest.TestCase):
    def test_trimming_the_oldest_events_keeps_sequence_numbers_stable(self):
        """A page that asks from an old seq must get the oldest event still held, not a gap or a
        repeat; seq is the page's cursor, so it can never be renumbered."""
        s = gui.Session("0" * 12, ".")
        with mock.patch.object(gui, "MAX_EVENTS", 10):
            for i in range(25):
                s.add({"kind": "log", "text": str(i)})
        seqs = [e["seq"] for e in s.events]
        self.assertEqual(seqs, list(range(s.base, s.base + len(s.events))))
        self.assertEqual(seqs[-1], 24)
        self.assertGreater(s.base, 0)
        self.assertLessEqual(len(s.events), 10)
        self.assertEqual(s.events[-1]["text"], "24")


class _Gone:
    """A harness process that has already exited and whose pipes are drained."""
    stdin = stdout = stderr = None

    def __init__(self, lines=()):
        self.stdout = _Pipe(lines)
        self.stdin = _Pipe(())
        self.pid = 0

    def wait(self):
        return 1

    def poll(self):
        return 1


class _Pipe:
    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


class TestStaleProcessEvents(unittest.TestCase):
    def test_an_old_process_cannot_end_or_speak_for_the_new_one(self):
        """stop() then start() can swap the process before the old reader thread has finished.
        Its late `exited` would show the running new harness as dead and wipe its open
        approval question (found by the second review)."""
        s = gui.Session("1" * 12, ".")
        old, new = _Gone([b'{"kind":"log","text":"late line from the old one"}\n']), _Gone()
        s.proc = new                                      # start() already replaced it
        s.add({"kind": "ask", "rid": "q1", "what": "x"})  # the new process asked something
        s._pump_out(old)                                  # ... and only now the old reader finishes
        kinds = [e["kind"] for e in s.events]
        self.assertNotIn("exited", kinds)
        self.assertNotIn("log", kinds)
        self.assertIn("q1", s.pending)                    # the open question survived
        s._pump_out(new)                                  # the current process ending still counts
        self.assertEqual(s.events[-1]["kind"], "exited")
        self.assertEqual(s.pending, {})

    def test_the_swap_cannot_happen_between_the_check_and_the_add(self):
        """The same guard, but the window the third review found: start() swaps the process
        while the old reader is between 'am I current?' and 'add the event'. The swap is held
        open here; the old reader must wait for it and then see that it is no longer current.
        Mutation: take the lock out of _add_if_current and this test fails."""
        s = gui.Session("2" * 12, ".")
        old, new = _Gone(), _Gone()
        s.proc = old
        sent = []
        with s.gen_lock:                                  # start() is in the middle of swapping
            t = threading.Thread(target=lambda: sent.append(
                s._add_if_current(old, {"kind": "exited", "code": 1})))
            t.start()
            time.sleep(0.3)                               # the old reader is at (or past) its check
            s.proc = new
        t.join(10)
        self.assertEqual(sent, [False])
        self.assertEqual(s.events, [])


class TestTerminalModeIsUnchanged(unittest.TestCase):
    """Without --jsonl, emit() must print what print() printed. Run in a real process: emit is
    the one place every terminal line now goes through."""

    def run_py(self, code):
        import subprocess
        out = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True,
                             text=True, encoding="utf-8", cwd=SRC, timeout=60,
                             env={**os.environ, "HARNESS_BASE_URL": "http://127.0.0.1:1/v1",
                                  "HARNESS_MODEL": "x"})
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_emit_prints_the_terminal_text_and_nothing_for_results(self):
        out = self.run_py(
            "import harness_vllm as hv\n"
            "hv.emit('tool_call', term='  [tool] f({})', id='c1', name='f', args='{}')\n"
            "hv.emit('tool_result', id='c1', text='SECRET-RESULT', chars=13)\n"
            "hv.emit('assistant_delta', term='ab', end='', flush=True, text='ab')\n"
            "hv.emit('assistant_end', term='')\n"
            "hv.emit('state', mode='normal')\n")
        self.assertEqual(out, "  [tool] f({})\nab\n")

    def test_jsonl_mode_turns_stray_prints_into_log_events(self):
        """One raw line on stdout would break the JSON stream the page reads."""
        out = self.run_py(
            "import sys, harness_vllm as hv\n"
            "hv.enter_jsonl()\n"
            "print('plain line')\n"
            "sys.stdout.write('part'); sys.stdout.write('ial\\n\\n')\n"
            "hv.emit('state', mode='safe')\n")
        lines = [json.loads(x) for x in out.splitlines()]
        self.assertEqual([(e["kind"], e.get("text")) for e in lines],
                         [("log", "plain line"), ("log", "partial"), ("state", None)])


class TestRealHarness(GuiBase):
    def test_a_turn_comes_out_as_events(self):
        """--jsonl: ready, the user line, the answer, usage, state and turn_end, in that order."""
        FakeLLM.script = [text_reply("hello back")]
        sid, s = self.new_session()
        self.wait(s, lambda e: e["kind"] == "ready", "ready")
        st, _ = self.call("POST", f"/api/sessions/{sid}/send", {"text": "hi there"})
        self.assertEqual(st, 200)
        self.wait(s, lambda e: e["kind"] == "turn_end", "turn_end")
        k = self.kinds(s)
        for a, b in (("ready", "user"), ("user", "turn_start"), ("turn_start", "usage"),
                     ("turn_start", "assistant"), ("assistant", "turn_end"),
                     ("usage", "turn_end")):
            self.assertLess(k.index(a), k.index(b), k)
        self.assertEqual(k[-2:], ["turn_end", "state"])           # the panel is refreshed after a turn
        a = next(e for e in s.events if e["kind"] == "assistant")
        self.assertEqual(a["text"], "hello back")
        u = next(e for e in s.events if e["kind"] == "usage")
        self.assertEqual((u["tokens_in"], u["tokens_out"]), (5, 1))
        state = [e for e in s.events if e["kind"] == "state"][-1]
        self.assertEqual(state["mode"], "normal")
        self.assertTrue(os.path.exists(s.file))               # the session file is outside the project
        self.assertFalse(os.path.exists(os.path.join(self.project, "session.json")))

    def test_non_ascii_survives_the_pipes(self):
        """cp950 in the middle would turn Chinese into question marks; the wire is ASCII JSON."""
        FakeLLM.script = [text_reply("收到：測試")]
        sid, s = self.new_session()
        self.wait(s, lambda e: e["kind"] == "ready", "ready")
        self.call("POST", f"/api/sessions/{sid}/send", {"text": "你好，世界"})
        a = self.wait(s, lambda e: e["kind"] == "assistant", "assistant")
        self.assertEqual(a["text"], "收到：測試")
        with open(s.file, encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["content"], "你好，世界")

    def test_every_line_the_harness_prints_is_valid_json(self):
        """Startup notes, slash output and any stray print() must become log events, or one
        raw line would corrupt the stream the page reads."""
        FakeLLM.script = []
        sid, s = self.new_session()
        self.wait(s, lambda e: e["kind"] == "ready", "ready")
        self.call("POST", f"/api/sessions/{sid}/send", {"text": "/help"})
        log = self.wait(s, lambda e: e["kind"] == "log" and "/compact" in e.get("text", ""), "help log")
        self.assertEqual([e for e in s.events if e["kind"] == "stderr"], [])
        self.assertIn("/mode", log["text"])

    def test_streamed_text_arrives_in_pieces_then_an_end(self):
        """The page appends deltas to one bubble and closes it on assistant_end."""
        with mock.patch.dict(os.environ, {"HARNESS_STREAM": "1"}):
            FakeLLM.script = [("sse", "streamed answer")]
            sid, s = self.new_session()
        self.wait(s, lambda e: e["kind"] == "ready", "ready")
        self.call("POST", f"/api/sessions/{sid}/send", {"text": "go"})
        self.wait(s, lambda e: e["kind"] == "assistant_end", "assistant_end")
        deltas = [e["text"] for e in s.events if e["kind"] == "assistant_delta"]
        self.assertGreaterEqual(len(deltas), 2)
        self.assertEqual("".join(deltas), "streamed answer")

    def test_a_tool_call_and_its_result_share_an_id(self):
        FakeLLM.script = [command_reply("echo tool-ran"), text_reply("done")]
        sid, s = self.new_session()
        self.wait(s, lambda e: e["kind"] == "ready", "ready")
        self.call("POST", f"/api/sessions/{sid}/send", {"text": "run it"})
        self.wait(s, lambda e: e["kind"] == "turn_end", "turn_end")
        call = next(e for e in s.events if e["kind"] == "tool_call")
        res = next(e for e in s.events if e["kind"] == "tool_result")
        self.assertEqual((call["id"], call["name"]), (res["id"], "run_command"))
        self.assertIn("tool-ran", res["text"])
        self.assertIn("echo tool-ran", call["args"])             # the full command, not a clipped line

    def test_an_approval_is_answered_by_its_id_and_nothing_else(self):
        """The core safety property of the GUI: a typed message, a stale id or a wrong id
        must never count as 'yes'. Mutation check: if any line answered, the first assertion fails."""
        FakeLLM.script = [command_reply("echo approved-run"), text_reply("finished"),
                          text_reply("the queued y")]
        sid, s = self.new_session(mode="safe")
        self.wait(s, lambda e: e["kind"] == "ready", "ready")
        self.call("POST", f"/api/sessions/{sid}/send", {"text": "do it"})
        ask = self.wait(s, lambda e: e["kind"] == "ask", "ask")
        self.assertIn("echo approved-run", ask["what"])
        self.assertTrue(ask["rid"])
        # a user message while the question is open: queued, NOT taken as the answer
        self.call("POST", f"/api/sessions/{sid}/send", {"text": "y"})
        time.sleep(1.0)
        self.assertNotIn("tool_result", self.kinds(s))
        st, _ = self.call("POST", f"/api/sessions/{sid}/answer", {"rid": "q999", "text": "y"})
        self.assertEqual(st, 409)
        self.assertNotIn("tool_result", self.kinds(s))
        st, _ = self.call("POST", f"/api/sessions/{sid}/answer", {"rid": ask["rid"], "text": "y"})
        self.assertEqual(st, 200)
        res = self.wait(s, lambda e: e["kind"] == "tool_result", "tool_result")
        self.assertIn("approved-run", res["text"])
        st, _ = self.call("POST", f"/api/sessions/{sid}/answer", {"rid": ask["rid"], "text": "y"})
        self.assertEqual(st, 409)                                # an id works once
        # the typed "y" was not lost: it ran as an ordinary message after the approval
        self.wait(s, lambda e: e.get("text") == "the queued y", "queued message answered")

    def test_the_harness_itself_drops_an_answer_to_another_question(self):
        """The server checks the id too, but the harness is the last line: it must not rely on
        its caller. Talk to the process directly, past the server's check."""
        FakeLLM.script = [command_reply("echo direct-run"), text_reply("finished")]
        sid, s = self.new_session(mode="safe")
        self.wait(s, lambda e: e["kind"] == "ready", "ready")
        self.call("POST", f"/api/sessions/{sid}/send", {"text": "do it"})
        ask = self.wait(s, lambda e: e["kind"] == "ask", "ask")
        self.assertTrue(s.send({"type": "answer", "rid": "q999", "text": "y"}))   # not the open question
        time.sleep(1.0)
        self.assertNotIn("tool_result", self.kinds(s))
        self.assertTrue(s.send({"type": "answer", "rid": ask["rid"], "text": "y"}))
        res = self.wait(s, lambda e: e["kind"] == "tool_result", "tool_result")
        self.assertIn("direct-run", res["text"])

    def test_declining_denies_the_call(self):
        FakeLLM.script = [command_reply("echo should-not-run"), text_reply("ok")]
        sid, s = self.new_session(mode="safe")
        self.wait(s, lambda e: e["kind"] == "ready", "ready")
        self.call("POST", f"/api/sessions/{sid}/send", {"text": "do it"})
        ask = self.wait(s, lambda e: e["kind"] == "ask", "ask")
        self.call("POST", f"/api/sessions/{sid}/answer", {"rid": ask["rid"], "text": "n"})
        res = self.wait(s, lambda e: e["kind"] == "tool_result", "tool_result")
        self.assertTrue(res["text"].startswith("DENIED"), res["text"])
        self.assertNotIn("should-not-run", res["text"])

    def test_stop_then_resume_continues_the_same_conversation(self):
        """Stop is a hard kill (the harness' crash recovery is what makes that safe); resume
        must give the model the earlier turn back and the page the history."""
        FakeLLM.script = [text_reply("first answer"), text_reply("second answer")]
        sid, s = self.new_session()
        self.wait(s, lambda e: e["kind"] == "ready", "ready")
        self.call("POST", f"/api/sessions/{sid}/send", {"text": "remember KESTREL-81"})
        self.wait(s, lambda e: e["kind"] == "turn_end", "turn_end")
        self.call("POST", f"/api/sessions/{sid}/stop", {})
        self.wait(s, lambda e: e["kind"] == "exited", "exited")
        self.assertFalse(s.running)
        st, _ = self.call("POST", f"/api/sessions/{sid}/send", {"text": "x"})
        self.assertEqual(st, 409)                                # nobody home: say so
        # a fresh server process knows the session only from the files
        fresh = gui.Registry().get(sid)
        self.assertEqual([e["kind"] for e in fresh.events], ["user", "assistant"])
        self.assertTrue(fresh.events[0]["history"])
        self.assertIn("remember KESTREL-81", fresh.events[0]["text"])
        n = len(s.events)
        st, _ = self.call("POST", f"/api/sessions/{sid}/resume", {})
        self.assertEqual(st, 200)
        self.wait(s, lambda e: e["kind"] == "ready" and e["seq"] >= n, "ready again")
        self.call("POST", f"/api/sessions/{sid}/send", {"text": "what was the code?"})
        self.wait(s, lambda e: e.get("text") == "second answer", "second answer")
        with open(s.file, encoding="utf-8") as f:
            roles = [m["role"] for m in json.load(f)]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])

    def test_simultaneous_resumes_start_one_harness(self):
        """Two tabs (or a double click) can send resume at once; a second process on the same
        session file that nobody tracks could not be stopped from the GUI and writes the same file."""
        sid, s = self.new_session()
        self.wait(s, lambda e: e["kind"] == "ready", "ready")
        s.stop()
        self.wait(s, lambda e: e["kind"] == "exited", "exited")
        n = len(s.events)
        gate = threading.Barrier(6)

        def go():
            gate.wait()
            s.start()
        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.wait(s, lambda e: e["kind"] == "ready" and e["seq"] >= n, "ready again")
        time.sleep(4)                                            # a second harness would say ready too
        readies = [e for e in s.events if e["kind"] == "ready" and e["seq"] >= n]
        self.assertEqual(len(readies), 1, self.kinds(s))

    def test_two_sessions_do_not_share_anything(self):
        """One process per session: the global state of the harness cannot leak across tabs."""
        FakeLLM.script = [text_reply("for a"), text_reply("for b")]
        a, sa = self.new_session()
        b, sb = self.new_session(mode="auto")
        self.wait(sa, lambda e: e["kind"] == "ready", "ready a")
        self.wait(sb, lambda e: e["kind"] == "ready", "ready b")
        self.assertNotEqual(sa.proc.pid, sb.proc.pid)
        ra = next(e for e in sa.events if e["kind"] == "ready")
        rb = next(e for e in sb.events if e["kind"] == "ready")
        self.assertEqual((ra["mode"], rb["mode"]), ("normal", "auto"))
        self.assertNotEqual(sa.file, sb.file)
        st, listing = self.call("GET", "/api/sessions")
        self.assertEqual({x["id"] for x in listing["sessions"]}, {a, b})

    def test_the_stream_replays_from_a_sequence_number(self):
        """A reloaded page asks for from=N and gets exactly the rest, as one JSON per line."""
        FakeLLM.script = [text_reply("x")]
        sid, s = self.new_session()
        self.wait(s, lambda e: e["kind"] == "ready", "ready")
        self.call("POST", f"/api/sessions/{sid}/send", {"text": "go"})
        self.wait(s, lambda e: e["kind"] == "turn_end", "turn_end")
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", f"/api/sessions/{sid}/stream?from=2",
                  headers={"Host": f"127.0.0.1:{self.port}", "Authorization": f"Bearer {TOKEN}"})
        r = c.getresponse()
        got = []
        while len(got) < len(s.events) - 2:
            line = r.fp.readline().strip()
            if line:
                got.append(json.loads(line))
        c.close()
        self.assertEqual([e["seq"] for e in got], list(range(2, len(s.events))))


if __name__ == "__main__":
    unittest.main()
