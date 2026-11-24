"""Run ONE question through a harness child process (stage S4; the three lines of contact, modelled on superagent's executor).

kb touches the harness through exactly three things and imports none of it: the child process `harness_vllm.py --jsonl <session file>`, the
`.harness.hooks.json` written into the ticket's own folder, and an environment built here. Everything that decides what the child may do is
fixed in this file, not passed in by the caller: the mode is `safe`, the environment is an allow-list plus pinned values, the ticket folder is
checked before every start, the hook vetoes every tool, and no MCP server is configured. A question that the model asks the person (an approval)
is answered `n`: the model has no tools, so nothing legitimate asks.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from . import ask_sanitize as sanitize
from .errors import KbError

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "ask_hook.py")
DEFAULT_HARNESS = "C:/Users/roy/roy_harness/src/harness/harness_vllm.py"
SYSTEM_KEEP = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP")
PIN_EMPTY = ("HARNESS_API_KEY", "VLLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HARNESS_DEBUG", "HARNESS_SKILLS_DIRS", "HARNESS_SANDBOX",
             "VLLM_BASE_URL", "VLLM_MODEL", "HARNESS_ALLOW_HTTP")
FORBIDDEN_IN_TICKET_DIR = (".harness_permissions.json", ".harness.env")
FAILURE_MARK = "model call failed"
REPLY_CAP = 4000


KILL_TIMEOUT = 30.0              # seconds a `taskkill` may take before the plain kill of the parent is used (seventh review)
MAX_TIMEOUT = 1800.0           # seconds; a longer `--timeout` is cut to this (commit removes working folders older than an hour as dead; sixth review)


class RunnerError(KbError):
    pass


@dataclass(frozen=True)
class RunnerConfig:
    base_url: str
    model: str
    provider: str = "vllm"
    harness_path: str = DEFAULT_HARNESS
    api_key_env: str = ""            # the NAME of an environment variable holding the key; its value is passed to the child and never printed
    timeout: float = 300.0
    allow_http: bool = False


@dataclass
class RunResult:
    text: object = None             # the model's last text of the turn, or None
    failure: object = None          # the harness's own line when the model call failed
    asked: int = 0                  # approval questions answered "n"
    timed_out: bool = False
    exited: object = None           # exit code when the process ended before the turn did
    events: int = 0
    stderr_tail: list = field(default_factory=list)


def build_env(cfg, parent=None):
    """The child's whole environment: an allow-list from the parent, then explicit values. The caller cannot add anything."""
    parent = os.environ if parent is None else parent
    upper = {k.upper(): v for k, v in parent.items()}
    env = {k: upper[k] for k in SYSTEM_KEEP if k in upper}
    env.update({name: "" for name in PIN_EMPTY})
    env.update({
        "HARNESS_PROVIDER": cfg.provider, "HARNESS_BASE_URL": cfg.base_url, "HARNESS_MODEL": cfg.model,
        "HARNESS_MODE": "safe", "HARNESS_STREAM": "0", "HARNESS_VISION": "0",
        "HARNESS_NOTES": "0",                 # notes are on by default; a question ticket must leave none behind
        "HARNESS_COMPACT_TOKENS": "96000", "HARNESS_KEEP_RECENT": "8", "HARNESS_BACKOFF": "1.0", "HARNESS_EOF_GRACE": "0.0",
        "PYTHONIOENCODING": "utf-8",
    })
    if cfg.allow_http:
        env["HARNESS_ALLOW_HTTP"] = "1"
    if cfg.api_key_env:
        key = parent.get(cfg.api_key_env, "")
        if not key:
            raise RunnerError(f"the environment variable {cfg.api_key_env} (named as the key) is empty or missing")
        env["HARNESS_API_KEY"] = key
    return env


def pinned_keys():
    probe = build_env(RunnerConfig("u", "m"), parent={})
    return frozenset(probe) | frozenset(PIN_EMPTY) | {"HARNESS_ALLOW_HTTP"}


def env_file_keys(path):
    """Keys of a `.harness.env`, parsed the way the harness parses it."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    return [line.split("=", 1)[0].strip() for line in (x.strip() for x in lines) if line and not line.startswith("#") and "=" in line]


def preflight(cwd, harness_path):
    """Fail closed before a start: something could widen what the child may do."""
    if not os.path.isfile(harness_path):
        raise RunnerError(f"the harness program is not at {harness_path}")
    root_env = os.path.join(os.path.dirname(os.path.abspath(harness_path)), "..", "..", ".harness.env")
    loose = sorted(k for k in env_file_keys(root_env) if k.upper() not in {p.upper() for p in pinned_keys()})
    if loose:
        raise RunnerError(f"{os.path.normpath(root_env)} has key(s) {loose} that the harness would load into the ticket's environment; pin or remove them")
    for name in FORBIDDEN_IN_TICKET_DIR:
        if os.path.exists(os.path.join(cwd, name)):
            raise RunnerError(f"{os.path.join(cwd, name)} exists: a ticket folder must not carry permissions or environment files")


def write_ticket_files(cwd, python=None, hook=HOOK):
    """The two config files the harness reads from its working folder, rewritten on every start. The hook is an exec-form command with absolute paths."""
    python = python or sys.executable
    hooks = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": python, "args": [hook], "timeout": 30, "onFailure": "block"}]}]}}
    with open(os.path.join(cwd, ".harness.hooks.json"), "w", encoding="utf-8") as f:
        json.dump(hooks, f, indent=1)
    with open(os.path.join(cwd, ".harness.mcp.json"), "w", encoding="utf-8") as f:
        json.dump({"servers": {}}, f)


def kill_tree(proc):
    """terminate() leaves grandchildren on Windows; the system taskkill by full path (a bare name would be looked up in the current folder first)."""
    if sys.platform == "win32":
        taskkill = os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=KILL_TIMEOUT)
        except subprocess.TimeoutExpired:         # a taskkill that hangs must not hold the question open past its deadline (seventh review, 2026-11-24)
            proc.kill()
    else:
        proc.kill()


class _Reply:
    """The reply of a turn is the text the model wrote AFTER its last tool event of that turn; text before a tool call was thinking aloud."""

    def __init__(self):
        self.text, self.failure = None, None

    def feed(self, ev):
        kind = ev.get("kind")
        if kind == "assistant":
            self.text = str(ev.get("text", ""))
        elif kind in ("turn_start", "tool_call", "tool_result"):
            self.text = None
            if kind == "turn_start":
                self.failure = None
        elif kind == "log" and FAILURE_MARK in str(ev.get("text", "")):
            self.failure = str(ev["text"]).strip()
        elif kind == "turn_end":
            return True
        return False


def _lines(pipe):
    try:
        for raw in pipe:
            yield raw
    except (OSError, ValueError):
        return


def run_once(cfg, cwd, session_file, message):
    """Start a harness child in `cwd`, send ONE message, wait for the end of the turn, stop the child. Returns a RunResult; never raises for a failure of the model."""
    preflight(cwd, cfg.harness_path)
    write_ticket_files(cwd)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([sys.executable, "-X", "utf8", cfg.harness_path, "--jsonl", session_file], cwd=cwd, env=build_env(cfg),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    events = queue.Queue()
    result = RunResult()

    def pump_out():
        for raw in _lines(proc.stdout):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            try:
                ev = json.loads(line)
                if not isinstance(ev, dict) or "kind" not in ev:
                    raise ValueError
            except ValueError:
                ev = {"kind": "log", "text": line}
            events.put(ev)
        events.put({"kind": "exited", "code": proc.wait()})

    def pump_err():
        for raw in _lines(proc.stderr):
            result.stderr_tail = (result.stderr_tail + [sanitize.clean_line(raw.decode("utf-8", "replace"))[:200]])[-5:]
    threading.Thread(target=pump_out, daemon=True).start()
    threading.Thread(target=pump_err, daemon=True).start()

    def send(obj):
        try:
            proc.stdin.write((json.dumps(obj) + "\n").encode("ascii"))
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False
    reply = _Reply()
    try:
        if not send({"type": "user", "text": message}):
            raise RunnerError("the harness process did not accept input")
        deadline = time.monotonic() + min(cfg.timeout, MAX_TIMEOUT)          # the erasure step relies on this: no ticket older than an hour is still live
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                result.timed_out = True
                break
            try:
                ev = events.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            result.events += 1
            if ev.get("kind") == "ask":
                send({"type": "answer", "rid": str(ev.get("rid")), "text": "n"})     # the model has no tools: nothing legitimate asks
                result.asked += 1
            elif ev.get("kind") == "exited":
                result.exited = ev.get("code")
                break
            if reply.feed(ev):
                break
        result.text, result.failure = reply.text, reply.failure
    finally:
        if proc.poll() is None:
            send({"type": "user", "text": "/quit"})
            end = time.monotonic() + 5.0
            while proc.poll() is None and time.monotonic() < end:
                time.sleep(0.05)
        if proc.poll() is None:
            kill_tree(proc)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                print(f"WARNING: the harness process {proc.pid} did not confirm that it stopped; its working folder is removed by the next `kb commit` "
                      "only once nothing holds it open.", file=sys.stderr)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
    return result
