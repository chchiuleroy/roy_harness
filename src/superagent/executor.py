"""The Executor port and its roy_harness implementation.

An Executor is "something that runs one ticket": give it a folder and a session file, send it
lines, read events back. HarnessExecutor does that with `harness_vllm.py --jsonl` as a child
process, exactly the way harness_gui.py does, and never imports the harness.

Everything that decides what the child may do is fixed here, not passed in by the caller:
the mode is `safe`, the environment is built from a short allow-list plus pinned values, and
the ticket folder is checked before every start.
"""
import dataclasses
import json
import os
import queue
import subprocess
import sys
import threading
import time

from . import procutil

HERE =os.path.dirname(os.path.abspath(__file__))
HARNESS_PATH = os.path.normpath(os.path.join(HERE, "..", "harness", "harness_vllm.py"))
MANDATE_HOOK = os.path.join(HERE, "mandate_hook.py")

# What the child needs just to start on Windows (CreateProcess, sockets, cmd.exe).
SYSTEM_KEEP = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
               "TEMP", "TMP")

# harness_vllm reads two `.harness.env` files with os.environ.setdefault, for EVERY key. A key
# that is already in the environment, even as an empty string, is never overwritten, and the
# harness tests all of these for truthiness, so "" means "off".
PIN_EMPTY = ("HARNESS_API_KEY", "VLLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
             "HARNESS_DEBUG", "HARNESS_SKILLS_DIRS", "HARNESS_SANDBOX",
             "VLLM_BASE_URL", "VLLM_MODEL", "HARNESS_ALLOW_HTTP")

FORBIDDEN_IN_TICKET_DIR = (".harness_permissions.json", ".harness.env")


class PreflightError(Exception):
    """A ticket must not start: something could widen what the child is allowed to do."""


@dataclasses.dataclass(frozen=True)
class ExecutorConfig:
    provider: str
    base_url: str
    model: str
    api_key: str = ""
    compact_tokens: int = 96000
    keep_recent: int = 8
    backoff: float = 1.0
    eof_grace: float = 0.0


def build_env(cfg, parent=None):
    """The child's whole environment. Allow-list from the parent, then explicit values; the
    caller cannot add anything (no `extra` argument on purpose)."""
    parent = os.environ if parent is None else parent
    upper = {k.upper(): v for k, v in parent.items()}
    env = {k: upper[k] for k in SYSTEM_KEEP if k in upper}
    env.update({name: "" for name in PIN_EMPTY})
    env.update({
        "HARNESS_PROVIDER": cfg.provider,
        "HARNESS_BASE_URL": cfg.base_url,
        "HARNESS_MODEL": cfg.model,
        "HARNESS_MODE": "safe",                 # fixed: a ticket never runs in normal or auto
        "HARNESS_STREAM": "0",
        "HARNESS_VISION": "0",
        "HARNESS_COMPACT_TOKENS": str(cfg.compact_tokens),
        "HARNESS_KEEP_RECENT": str(cfg.keep_recent),
        "HARNESS_BACKOFF": str(cfg.backoff),
        "HARNESS_EOF_GRACE": str(cfg.eof_grace),
        "PYTHONIOENCODING": "utf-8",
    })
    if cfg.api_key:
        env["HARNESS_API_KEY"] = cfg.api_key
    return env


def pinned_keys():
    """Every key build_env sets. A `.harness.env` key outside this set would be loaded into
    the child with setdefault and nobody would have chosen it."""
    probe = build_env(ExecutorConfig("p", "u", "m", api_key="k"), parent={})
    return frozenset(probe) | frozenset(PIN_EMPTY)


def env_file_keys(path):
    """Keys of a `.harness.env`, parsed the way harness_vllm._load_local_env parses it."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    keys = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].strip())
    return keys


def preflight(cwd, harness_path=HARNESS_PATH):
    """Fail closed before a start. Raises PreflightError."""
    root_env = os.path.join(os.path.dirname(os.path.abspath(harness_path)), "..", "..", ".harness.env")
    pinned = {k.upper() for k in pinned_keys()}
    for path in (root_env,):
        loose = sorted(k for k in env_file_keys(path) if k.upper() not in pinned)
        if loose:
            raise PreflightError(f"{os.path.normpath(path)} has key(s) {loose} that the harness "
                                 f"would load into the ticket's environment; pin them or remove them")
    for name in FORBIDDEN_IN_TICKET_DIR:
        if os.path.exists(os.path.join(cwd, name)):
            raise PreflightError(f"{os.path.join(cwd, name)} exists: a ticket folder must not "
                                 f"carry permissions or environment files")


def write_ticket_files(cwd, mandate_path, python=None, mandate_hook=MANDATE_HOOK):
    """The two config files the harness reads from its working folder. Rewritten on every
    start. The hook is an exec-form command with absolute paths: no PATH lookup, no
    environment variable needed to find the mandate."""
    python = python or sys.executable
    hooks = {"hooks": {"PreToolUse": [{"hooks": [{
        "type": "command", "command": python,
        "args": [mandate_hook, "--mandate", mandate_path],
        "timeout": 30, "onFailure": "block"}]}]}}
    with open(os.path.join(cwd, ".harness.hooks.json"), "w", encoding="utf-8") as f:
        json.dump(hooks, f, indent=1)
    with open(os.path.join(cwd, ".harness.mcp.json"), "w", encoding="utf-8") as f:
        json.dump({"servers": {}}, f)


def kill_tree(proc):
    """terminate() leaves grandchildren on Windows. The system taskkill by full path: a bare
    name would be looked up in the current folder first."""
    if sys.platform == "win32":
        taskkill = os.path.join(os.environ.get("SystemRoot", "C:/Windows"),
                               "System32", "taskkill.exe")
        subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()


def kill_pid(pid):
    """Kill a process tree by pid (used on processes an earlier orchestrator left behind)."""
    class _P:
        pass
    proc = _P()
    proc.pid = int(pid)
    proc.kill = lambda: os.kill(int(pid), 9)
    kill_tree(proc)


class HarnessExecutor:
    """One harness child process for one ticket."""

    def __init__(self, cfg, harness_path=HARNESS_PATH):
        self.cfg, self.harness_path = cfg, harness_path
        self.events = queue.Queue()
        self._seq = 0                       # every event gets the next number as it is queued
        self._seq_lock = threading.Lock()
        self.last_answer_mark = 0           # the number of the last event queued before an answer was written
        self.proc = None
        self.create_time = None
        self._write_lock = threading.Lock()

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, cwd, session_file):
        if self.alive:
            raise RuntimeError("already running")
        preflight(cwd, self.harness_path)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", self.harness_path, "--jsonl", session_file],
            cwd=cwd, env=build_env(self.cfg), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=flags)
        self.proc = proc
        self.create_time = procutil.create_time(proc.pid)
        threading.Thread(target=self._pump_out, args=(proc,), daemon=True).start()
        threading.Thread(target=self._pump_err, args=(proc,), daemon=True).start()

    @staticmethod
    def _lines(pipe):
        """Lines of a child's pipe. stop() closes the pipe while the pump threads are reading it; on
        Windows that makes the read raise (ValueError 'PyMemoryView_FromBuffer(): info->buf must not be
        NULL', or OSError). The pipe is simply finished then, so end quietly instead of printing a traceback."""
        try:
            for raw in pipe:
                yield raw
        except (OSError, ValueError):
            return

    def _pump_out(self, proc):
        for raw in self._lines(proc.stdout):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            try:
                ev = json.loads(line)
                if not isinstance(ev, dict) or "kind" not in ev:
                    raise ValueError
            except ValueError:
                ev = {"kind": "log", "text": line}
            self._emit(dict(ev, _pid=proc.pid))
        code = proc.wait()
        self._emit({"kind": "exited", "code": code, "_pid": proc.pid})

    def _pump_err(self, proc):
        for raw in self._lines(proc.stderr):
            self._emit({"kind": "stderr", "text": raw.decode("utf-8", "replace").rstrip("\r\n"),
                        "_pid": proc.pid})

    def _emit(self, ev):
        """Queue an event with the next sequence number (`_seq`). Numbering and queuing are one step
        under a lock, so a number says exactly where an event sits relative to an answer."""
        with self._seq_lock:
            self._seq += 1
            ev["_seq"] = self._seq
            self.events.put(ev)

    def _send_answer(self, obj):
        """Write an answer and note which events were already queued at that moment. The lock is
        held across the write, so no event can be queued between 'noting' and 'writing': every event
        numbered after the mark was queued after the write began. Holding the lock over a pipe write is
        safe because the harness child reads stdin on its own thread into an unbounded queue (see
        start_reader in harness_vllm.py), whatever its stdout is doing: the write cannot wait on our
        pumps, so there is no cycle. (Review 5 suspected one; a child that is frozen blocks any write.)"""
        with self._seq_lock:
            self.last_answer_mark = self._seq
            return self._send_raw(obj)

    def _send_raw(self, obj):
        data = (json.dumps(obj) + "\n").encode("ascii")      # ensure_ascii: no codepage in between
        with self._write_lock:
            if not self.alive:
                return False
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                return False
        return True

    def send(self, text):
        """One user line. False when the process is not running."""
        return self._send_raw({"type": "user", "text": text})

    def answer(self, rid, yes):
        """Only y or n. An `a` would be written into the harness' permanent grants."""
        return self._send_answer({"type": "answer", "rid": str(rid), "text": "y" if yes else "n"})

    def answer_empty(self, rid):
        """Close a free-text question (ask_user) with an empty answer; nobody may type one."""
        return self._send_answer({"type": "answer", "rid": str(rid), "text": ""})

    def stop(self, force=False, grace=5.0):
        proc = self.proc
        if proc is None:
            return
        if not force and proc.poll() is None:
            self._send_raw({"type": "user", "text": "/quit"})
            deadline = time.time() + grace
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
        if proc.poll() is None:
            kill_tree(proc)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
