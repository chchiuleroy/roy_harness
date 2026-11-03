"""Shared test support for the superagent tests: a scripted OpenAI-compatible endpoint and a
few helpers for driving a real harness child process."""
import http.server
import json
import os
import queue
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class FakeLLM:
    """Replies from a script, one entry per POST. An entry is
         {"text": "..."}                         a final answer
         {"calls": [("read_file", {...}), ...]}  tool calls (the harness runs them, then asks again)
         {"status": 503}                         an HTTP error
    When the script runs out it answers "(script exhausted)" so a test never hangs on the model.
    Every request is kept: `requests` (bodies) and `headers`."""

    def __init__(self, script=()):
        self.script = list(script)
        self.requests, self.headers = [], []
        self._lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                with outer._lock:
                    outer.requests.append(body)
                    outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                    entry = outer.script.pop(0) if outer.script else {"text": "(script exhausted)"}
                if "status" in entry:
                    self.send_response(entry["status"])
                    self.end_headers()
                    self.wfile.write(b'{"error":"scripted"}')
                    return
                msg = {"role": "assistant", "content": entry.get("text")}
                finish = "stop"
                if entry.get("calls"):
                    finish = "tool_calls"
                    msg["tool_calls"] = [
                        {"id": f"call_{len(outer.requests)}_{i}", "type": "function",
                         "function": {"name": name, "arguments": json.dumps(args)}}
                        for i, (name, args) in enumerate(entry["calls"])]
                out = json.dumps({"choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def wait_for(events, kind, timeout=30.0, pred=None):
    """Next event of `kind` (that satisfies pred) from an Executor's queue; other events are
    returned in `seen`. Raises AssertionError on timeout, with what was seen."""
    seen, deadline = [], time.time() + timeout
    while time.time() < deadline:
        try:
            ev = events.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(ev)
        if ev.get("kind") == kind and (pred is None or pred(ev)):
            return ev, seen
        if ev.get("kind") == "exited":
            break
    raise AssertionError(f"no {kind!r} event; saw {[e.get('kind') for e in seen]} "
                         f"{[e for e in seen if e.get('kind') in ('log', 'stderr')][-5:]}")


def copy_harness_tree(dst):
    """A private copy of src/harness, laid out like the repo (dst/src/harness), so a test can put
    a .harness.env at dst/ without touching the real repository. -> path of harness_vllm.py."""
    target = os.path.join(dst, "src", "harness")
    os.makedirs(target)
    src = os.path.join(SRC, "harness")
    for name in os.listdir(src):
        if name.endswith(".py"):
            shutil.copy2(os.path.join(src, name), os.path.join(target, name))
    return os.path.join(target, "harness_vllm.py")
