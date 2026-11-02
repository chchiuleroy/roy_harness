#!/usr/bin/env python3
"""Local web GUI for roy_harness: many sessions in tabs, one harness process per session.

    python src/harness/harness_gui.py [--port 8791] [--no-browser]

The GUI never imports the harness. Each session is `harness_vllm.py --jsonl <file>` run as a
child process in the project folder you picked: structured events come out of its stdout, one
JSON object per line, and commands go into its stdin. A child that dies (or is stopped) leaves a
session file that the harness' own crash recovery can resume.

This server can make the harness run shell commands, so it is built like a remote-control
endpoint even though it listens on 127.0.0.1 only: a random token on every API call, a Host
and an Origin check (a web page open in your browser can talk to localhost), JSON-only POSTs,
and a CSP with a per-request nonce. The page itself writes model output with textContent only.

Standard library only, like the harness.
"""
import argparse
import base64
import binascii
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

import harness_hooks               # kill_tree: the same Windows-safe tree kill the harness uses

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "harness_vllm.py")
PAGE = os.path.join(HERE, "harness_gui.html")
MARKDOWN_JS = os.path.join(HERE, "harness_gui_md.js")   # its own file: Node tests it without a browser
HOME = os.environ.get("HARNESS_GUI_HOME") or os.path.join(os.path.expanduser("~"), ".roy_harness_gui")
SESSIONS = os.path.join(HOME, "sessions")   # outside every project: the model cannot write there
MAX_EVENTS = 20_000                         # per session; the oldest are dropped, seq stays stable
MAX_BODY = 1_000_000
MAX_UPLOAD_BODY = 8_000_000                 # a picture (up to MAX_PICTURE) as base64 inside JSON
MAX_PICTURE = 5_000_000                     # = the harness' MAX_IMAGE_BYTES; the harness decides, this is the first filter
PICTURES_PER_MESSAGE = 4
PICTURE_ID = re.compile(r"^[0-9a-f]{64}\.(png|jpg|gif|webp)$")
CLIP = 4_000
SID = re.compile(r"^[0-9a-f]{12}$")
# what the harness itself writes into user-role messages (see harness_vllm.py: compact, remind)
COMPACTED = "Earlier conversation was compacted."
WAKE_NOTE = "(No new user message"
REMINDERS = re.compile(r"\A(?:\s*<system-reminder>.*?</system-reminder>)+\s*", re.S)
MODES = ("safe", "normal", "auto")

# ---------------------------------------------------------------- sessions
def picture_kind(data):
    """'png' / 'jpg' / 'gif' / 'webp' from the first bytes, or None. Only a first filter: the harness
    checks the whole file (header, end, size, pixels) before a model ever sees it."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None

def messages_to_events(messages):
    """A session file as the same events a live run produces (what the page replays on resume)."""
    out = []
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, list):
            pictures = [p for p in content if isinstance(p, dict) and p.get("type") == "image_ref"]
            if pictures:
                # the text part is either what the person typed with the pictures, or only the
                # harness' own reminder (view_image): then there is nothing of the person's to show
                words = REMINDERS.sub("", " ".join(p.get("text", "") for p in content
                                                   if isinstance(p, dict) and p.get("type") == "text"), count=1)
                if words.strip() and not words.lstrip().startswith(WAKE_NOTE):
                    out.append({"kind": "user", "text": words[:20_000], "history": True})
                out += [{"kind": "image", "name": p.get("name"), "path": p.get("path"),
                         "width": p.get("width"), "height": p.get("height"), "history": True}
                        for p in pictures]
                continue
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = content if isinstance(content, str) else ""
        if role == "user":
            if content.startswith("<system-reminder>\n" + COMPACTED):   # the harness' own summary of what was cut
                out.append({"kind": "summary", "text": content[:CLIP * 2], "history": True})
                continue
            typed = REMINDERS.sub("", content, count=1)    # reminders the harness glued in front
            if typed != content and (not typed.strip() or typed.lstrip().startswith(WAKE_NOTE)):
                out.append({"kind": "notice", "text": content[:600], "history": True})
                continue
            out.append({"kind": "user", "text": typed[:20_000], "history": True})
        elif role == "assistant":
            if content.strip():
                out.append({"kind": "assistant", "text": content, "history": True})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                out.append({"kind": "tool_call", "id": tc.get("id"), "name": fn.get("name"),
                            "args": str(fn.get("arguments") or "")[:CLIP], "history": True})
        elif role == "tool":
            out.append({"kind": "tool_result", "id": m.get("tool_call_id"),
                        "text": content[:CLIP], "history": True})
    return out

class Session:
    def __init__(self, sid, cwd, mode="normal", title="", created=None):
        self.id, self.cwd, self.mode, self.title = sid, cwd, mode, title
        self.created = created or time.time()
        self.cond = threading.Condition()
        self.events, self.base = [], 0
        self.pending = {}                    # rid -> the ask event still waiting for an answer
        self.proc = None
        self.write_lock = threading.Lock()
        self.start_lock = threading.Lock()   # two resume requests must not start two harnesses
        self.pictures_lock = threading.Lock()
        self.gen_lock = threading.Lock()     # "is this still the current process" + "add its event" are one step

    file =property(lambda self: os.path.join(SESSIONS, self.id + ".json"))
    meta_file = property(lambda self: os.path.join(SESSIONS, self.id + ".meta.json"))
    running = property(lambda self: self.proc is not None and self.proc.poll() is None)

    def add(self, ev):
        with self.cond:
            ev["seq"] = self.base + len(self.events)
            self.events.append(ev)
            if len(self.events) > MAX_EVENTS:
                drop = MAX_EVENTS // 10
                del self.events[:drop]
                self.base += drop
            if ev.get("kind") == "ask":
                self.pending[ev.get("rid")] = ev
            elif ev.get("kind") in ("answered", "exited"):
                if ev.get("kind") == "exited":
                    self.pending.clear()
                else:
                    self.pending.pop(ev.get("rid"), None)
            self.cond.notify_all()

    def save_meta(self):
        os.makedirs(SESSIONS, exist_ok=True)
        tmp = self.meta_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"id": self.id, "cwd": self.cwd, "mode": self.mode, "title": self.title,
                       "created": self.created}, f, ensure_ascii=False)
        os.replace(tmp, self.meta_file)

    def summary(self):
        try:
            updated = os.path.getmtime(self.file)
        except OSError:
            updated = self.created
        return {"id": self.id, "cwd": self.cwd, "mode": self.mode, "title": self.title,
                "created": self.created, "updated": updated, "running": self.running,
                "waiting": len(self.pending), "events": self.base + len(self.events)}

    def start(self):
        with self.start_lock:
            if self.running:
                return
            env = dict(os.environ, HARNESS_MODE=self.mode, PYTHONIOENCODING="utf-8")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                [sys.executable, "-X", "utf8", HARNESS, "--jsonl", self.file], cwd=self.cwd,
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=flags)
            with self.gen_lock:                              # from here the old process is stale
                self.proc = proc
            threading.Thread(target=self._pump_out, args=(proc,), daemon=True).start()
            threading.Thread(target=self._pump_err, args=(proc,), daemon=True).start()

    def _add_if_current(self, proc, ev):
        """Add `ev` only if `proc` is still the session's process. The check and the add happen
        under the lock that start() takes to swap the process, so a late event from an old
        process can never slip in after the swap."""
        with self.gen_lock:
            if self.proc is not proc:
                return False
            self.add(ev)
            return True

    def _pump_out(self, proc):
        """Only the current process may speak for the session: stop() then start() can swap
        self.proc while this thread still drains the old one, and a late `exited` would show
        the running new harness as dead and wipe its open question."""
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            try:
                ev = json.loads(line)
                if not isinstance(ev, dict) or "kind" not in ev:
                    raise ValueError
            except ValueError:
                ev = {"kind": "log", "text": line}
            self._add_if_current(proc, ev)
        code = proc.wait()
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        self._add_if_current(proc, {"kind": "exited", "code": code})

    def _pump_err(self, proc):
        for raw in proc.stderr:
            self.add({"kind": "stderr", "text": raw.decode("utf-8", "replace").rstrip("\r\n")})
        proc.stderr.close()

    def send(self, obj):
        """One command line into the harness. False when it is not running."""
        data = (json.dumps(obj) + "\n").encode("ascii")      # ensure_ascii: no codepage in between
        with self.write_lock:
            if not self.running:
                return False
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError):                    # broken pipe, or already closed
                return False
        return True

    images_dir = property(lambda self: os.path.join(SESSIONS, self.id + ".images"))

    def keep_picture(self, name, data):
        """Store an uploaded picture under its own hash, next to the session and outside every
        project (the model's write_file cannot reach it). -> {"id", "name", "bytes"}."""
        kind = picture_kind(data)
        if kind is None:
            raise ValueError("not a PNG, JPEG, GIF or WebP picture")
        if len(data) > MAX_PICTURE:
            raise ValueError(f"the picture is {len(data)} bytes; the limit is {MAX_PICTURE}")
        pid = hashlib.sha256(data).hexdigest() + "." + kind
        clean = "".join(c for c in os.path.basename(str(name or "").replace("\\", "/")) if c.isprintable())[:80] or pid
        with self.pictures_lock:
            os.makedirs(self.images_dir, exist_ok=True)
            path = os.path.join(self.images_dir, pid)
            if not os.path.exists(path):
                with open(path + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(path + ".tmp", path)
            names = self._picture_names()
            names[pid] = clean
            with open(os.path.join(self.images_dir, "names.json.tmp"), "w", encoding="utf-8") as f:
                json.dump(names, f, ensure_ascii=False)
            os.replace(os.path.join(self.images_dir, "names.json.tmp"), os.path.join(self.images_dir, "names.json"))
        return {"id": pid, "name": clean, "bytes": len(data)}

    def _picture_names(self):
        try:
            with open(os.path.join(self.images_dir, "names.json"), encoding="utf-8") as f:
                names = json.load(f)
            return names if isinstance(names, dict) else {}
        except (OSError, ValueError):
            return {}

    def picture_items(self, ids):
        """[{"path", "name"}] for ids this session was given by keep_picture; ValueError for anything else."""
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("images must be a list of ids")
        if len(ids) > PICTURES_PER_MESSAGE:
            raise ValueError(f"at most {PICTURES_PER_MESSAGE} pictures go with one message")
        names = self._picture_names()
        items = []
        for pid in ids:
            path = os.path.join(self.images_dir, pid)
            if not PICTURE_ID.match(pid) or not os.path.isfile(path):
                raise ValueError(f"no such picture: {pid[:70]}")
            items.append({"path": path, "name": names.get(pid, pid)})
        return items

    def say(self, text, images=None):
        text = str(text)[:20_000]
        items = self.picture_items(images) if images else []
        if items and text.startswith("/"):
            raise ValueError("a command takes no picture")
        command = {"type": "user", "text": text}
        if items:
            command["images"] = items
        if not self.send(command):
            return False
        echo = {"kind": "user", "text": text}
        if text.startswith("/"):
            echo["slash"] = True                         # a command, not something to show as a speech bubble
        if items:
            echo["images"] = [i["name"] for i in items]
        self.add(echo)
        if not self.title and not text.startswith("/"):
            first = text.strip().splitlines()[0][:60] if text.strip() else ""
            self.title = first or ("[圖片] " + items[0]["name"] if items else "")
            if self.title:
                self.save_meta()
        return True

    def answer(self, rid, text):
        if rid not in self.pending:
            return False
        if not self.send({"type": "answer", "rid": rid, "text": str(text)[:20_000]}):
            return False
        self.add({"kind": "answered", "rid": rid})
        return True

    def stop(self):
        if self.running:
            harness_hooks.kill_tree(self.proc)

class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.live = {}

    def create(self, cwd, mode):
        sid = secrets.token_hex(6)
        s = Session(sid, cwd, mode)
        s.save_meta()
        with self.lock:
            self.live[sid] = s
        return s

    def get(self, sid):
        if not SID.match(sid or ""):
            return None
        with self.lock:
            s = self.live.get(sid)
            if s:
                return s
            try:
                with open(os.path.join(SESSIONS, sid + ".meta.json"), encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                return None
            s = Session(sid, m.get("cwd", os.getcwd()), m.get("mode", "normal"),
                        m.get("title", ""), m.get("created"))
            try:
                with open(s.file, encoding="utf-8") as f:
                    for ev in messages_to_events(json.load(f)):
                        s.add(ev)
            except (OSError, ValueError):
                pass
            self.live[sid] = s
            return s

    def listing(self):
        found = {}
        if os.path.isdir(SESSIONS):
            for name in os.listdir(SESSIONS):
                if name.endswith(".meta.json") and SID.match(name[:-10]):
                    s = self.get(name[:-10])
                    if s:
                        found[s.id] = s.summary()
        return sorted(found.values(), key=lambda x: x["updated"], reverse=True)

    def stop_all(self):
        with self.lock:
            sessions = list(self.live.values())
        for s in sessions:
            if s.running:
                s.send({"type": "user", "text": "/quit"})
        deadline = time.time() + 5
        for s in sessions:
            while s.running and time.time() < deadline:
                time.sleep(0.1)
            s.stop()

REGISTRY = Registry()

# ---------------------------------------------------------------- http
class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "harness-gui"
    timeout = 30                    # a body or a request line that never finishes frees its thread

    def log_message(self, fmt, *args):
        pass

    # --- guards
    def _guard(self, api):
        srv = self.server
        if self.headers.get("Host", "") not in srv.hosts:            # DNS rebinding
            return self._fail(403, "bad host")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in srv.origins:         # another web page
            return self._fail(403, "bad origin")
        if api:
            auth = self.headers.get("Authorization", "")
            if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], srv.token)):
                return self._fail(401, "token required")
            if self.command == "POST" and \
                    self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._fail(415, "application/json only")
        return True

    def _fail(self, code, why):
        self._json({"error": why}, code)
        return False

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --- routes
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/":
            if self._guard(False):
                self._page()
            return
        if url.path == "/md.js":                         # static, no secrets: the host check is enough
            if self._guard(False):
                self._static(MARKDOWN_JS, "application/javascript; charset=utf-8")
            return
        if not self._guard(True):
            return
        parts = url.path.strip("/").split("/")
        if parts == ["api", "sessions"]:
            sessions = REGISTRY.listing()
            recent = list(dict.fromkeys(s["cwd"] for s in sessions))[:10]
            return self._json({"sessions": sessions, "default_cwd": self.server.default_cwd,
                               "recent_cwds": recent})
        if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "stream":
            s = REGISTRY.get(parts[2])
            if not s:
                return self._fail(404, "no such session")
            try:
                start = int(urllib.parse.parse_qs(url.query).get("from", ["0"])[0])
            except ValueError:
                start = 0
            return self._stream(s, start)
        self._fail(404, "not found")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        # read the body before any refusal: closing on unread data resets the connection on
        # Windows and the client then never sees the 401/403 (a flaky test found this)
        limit = MAX_UPLOAD_BODY if self.path.split("?")[0].endswith("/upload") else MAX_BODY
        raw = self.rfile.read(n) if 0 <= n <= limit else None
        if not self._guard(True):
            return
        if raw is None:
            return self._fail(413, "bad body size")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._fail(400, "bad json")
        if not isinstance(body, dict):
            return self._fail(400, "object expected")
        parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
        if parts == ["api", "sessions"]:
            named = str(body.get("cwd") or "").strip()
            if not named:                                # realpath("") is the server's own folder
                return self._fail(400, "folder is required")
            cwd = os.path.realpath(named)
            mode = body.get("mode", "normal")
            if not os.path.isdir(cwd):
                return self._fail(400, f"not a folder: {cwd}")
            if mode not in MODES:
                return self._fail(400, "bad mode")
            s = REGISTRY.create(cwd, mode)
            s.start()
            return self._json(s.summary())
        if len(parts) == 4 and parts[:2] == ["api", "sessions"]:
            s = REGISTRY.get(parts[2])
            if not s:
                return self._fail(404, "no such session")
            act = parts[3]
            if act == "resume":
                if not os.path.isdir(s.cwd):
                    return self._fail(400, f"the project folder is gone: {s.cwd}")
                s.start()
                return self._json(s.summary())
            if act == "upload":
                try:
                    data = base64.b64decode(str(body.get("data") or ""), validate=True)
                    return self._json(s.keep_picture(body.get("name"), data))
                except (binascii.Error, ValueError) as e:
                    return self._fail(400, str(e))
            if act == "send":
                text = str(body.get("text") or "")
                images = body.get("images")
                if not text.strip() and not images:
                    return self._fail(400, "empty")
                try:
                    return self._json({"ok": True}) if s.say(text, images) else self._fail(409, "not running")
                except ValueError as e:
                    return self._fail(400, str(e))
            if act == "answer":
                ok = s.answer(str(body.get("rid") or ""), body.get("text", ""))
                return self._json({"ok": True}) if ok else self._fail(409, "no such question")
            if act == "stop":
                s.stop()
                return self._json({"ok": True})
        self._fail(404, "not found")

    def _static(self, path, content_type):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _page(self):
        nonce = secrets.token_urlsafe(16)
        with open(PAGE, encoding="utf-8") as f:
            body = f.read().replace("__NONCE__", nonce).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         f"default-src 'none'; script-src 'nonce-{nonce}'; "
                         f"style-src 'nonce-{nonce}'; connect-src 'self'; img-src blob:; base-uri 'none'; "
                         f"form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, s, start):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        i = start
        while True:
            with s.cond:
                i = max(i, s.base)
                if i - s.base >= len(s.events):
                    # Nothing more comes until it runs again (a session loaded from disk, or one
                    # whose `exited` the page now has): end the stream. The page then parks instead
                    # of holding one of the browser's six connections to this host for a stopped
                    # session; with six of those open, no request (send, answer, stop) got through.
                    if not s.running and (s.proc is None or (s.events and s.events[-1].get("kind") == "exited")):
                        return
                    s.cond.wait(15)
                batch = list(s.events[i - s.base:])
            try:
                if not batch:
                    self.wfile.write(b"\n")                  # heartbeat; also notices a closed tab
                else:
                    self.wfile.write("".join(json.dumps(ev) + "\n" for ev in batch).encode("ascii"))
                self.wfile.flush()
            except OSError:
                return
            i += len(batch)

def make_server(port, token, default_cwd):
    srv = Server(("127.0.0.1", port), Handler)
    port = srv.server_address[1]
    srv.token, srv.default_cwd = token, default_cwd
    srv.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    srv.origins = {"http://" + h for h in srv.hosts}
    return srv

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    token = secrets.token_urlsafe(24)
    srv = make_server(args.port, token, os.getcwd())
    url = f"http://127.0.0.1:{srv.server_address[1]}/#t={token}"
    print(f"roy_harness GUI: {url}\n(the token is new at every start; Ctrl-C stops all sessions)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        REGISTRY.stop_all()
        srv.server_close()

if __name__ == "__main__":
    main()
