"""Hooks: user-supplied reflexes bound to harness events (book ch 11).

A hook can OBSERVE, AUGMENT (its text reaches the model) or VETO. It can never
GRANT: an `allow` from a hook is ignored, so the gate, the modes and the human
stay in charge. The config shape and the exit-code contract follow Claude Code's
hooks (exit 0 = ok, exit 2 = block, JSON `permissionDecision: deny` or
`decision: block`), so existing scripts mostly run unchanged.

Differences from Claude Code, on purpose:
  * a hook that fails on a PRE event (PreToolUse, UserPromptSubmit) makes the
    harness ASK the human by default (onFailure "ask"); Claude Code proceeds
  * exec form only: `command` is a program and `args` its arguments, no shell
  * the hook's environment is scrubbed of KEY/TOKEN/SECRET/... names unless the
    config lists them under `env`; UTF-8 is forced so Chinese survives cp950 pipes
  * Stop only observes (Claude Code's exit 2 would keep the turn going)
  * the hooks file is local only: a hook is code, so it is never fetched
"""
import dataclasses
import json
import math
import os
import re
import subprocess
import sys

import harness_net

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
TOOL_EVENTS = ("PreToolUse", "PostToolUse")        # `matcher` applies to the tool name
PRE_EVENTS = ("PreToolUse", "UserPromptSubmit")    # these can veto, and ask on failure
OUTPUT_CAP = 10_000                                # chars per hook output
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 600.0                                # seconds; also rejects inf/nan
CONFIG_FILE = ".harness.hooks.json"
ON_FAILURE = ("ask", "block", "continue")
SCRUB = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)   # same rule as clean_env
# Claude Code fields we accept in a pasted config but do not implement.
IGNORED_KEYS = {"if", "async", "asyncRewake", "shell", "statusMessage", "once"}
COMMAND_KEYS = {"type", "command", "args", "env", "timeout", "onFailure"}
HTTP_KEYS = {"type", "url", "headers_env", "timeout", "onFailure", "allow_http"}


class HooksConfigError(ValueError):
    pass


@dataclasses.dataclass
class Handler:
    type: str
    on_failure: str
    timeout: float
    command: str = ""
    args: tuple = ()
    env: tuple = ()
    url: str = ""
    headers_env: dict = dataclasses.field(default_factory=dict)
    allow_http: bool = False

    def describe(self):
        what = (" ".join([self.command, *self.args]) if self.type == "command"
                else self.url)
        return f"{self.type}: {what} (onFailure={self.on_failure})"


@dataclasses.dataclass
class Group:
    matcher: object                 # compiled regex or None
    handlers: list


@dataclasses.dataclass
class Result:
    blocked: bool = False
    reason: str = ""
    context: list = dataclasses.field(default_factory=list)   # text for the model
    failures: list = dataclasses.field(default_factory=list)  # ask the human about these


class HooksConfig:
    def __init__(self, groups):
        self.groups = groups        # event -> [Group]

    def is_empty(self):
        return not any(self.groups.values())

    def describe(self):
        lines = []
        for event in EVENTS:
            for g in self.groups.get(event, []):
                pat = g.matcher.pattern if g.matcher is not None else "*"
                for h in g.handlers:
                    lines.append(f"{event} [{pat}] {h.describe()}")
        return lines


EMPTY = HooksConfig({})


def _fail(msg):
    raise HooksConfigError(msg)


def _parse_handler(event, raw, where):
    if not isinstance(raw, dict):
        _fail(f"{where}: a hook must be an object")
    kind = raw.get("type")
    if kind not in ("command", "http"):
        _fail(f"{where}: type must be 'command' or 'http', got {kind!r}")
    known = (COMMAND_KEYS if kind == "command" else HTTP_KEYS) | IGNORED_KEYS
    unknown = set(raw) - known
    if unknown:
        _fail(f"{where}: unknown key(s) {sorted(unknown)} (a typo here would "
              f"silently weaken a safety hook)")
    on_failure = raw.get("onFailure", "ask" if event in PRE_EVENTS else "continue")
    if on_failure not in ON_FAILURE:
        _fail(f"{where}: onFailure must be one of {ON_FAILURE}, got {on_failure!r}")
    timeout = raw.get("timeout", DEFAULT_TIMEOUT)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT):
        _fail(f"{where}: timeout must be a number of seconds, over 0 and at most {MAX_TIMEOUT:g}")
    if kind == "command":
        command, args = raw.get("command"), raw.get("args", [])
        if not isinstance(command, str) or not command.strip():
            _fail(f"{where}: command is required")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            _fail(f"{where}: args must be a list of strings")
        if not args and len(command.split()) > 1:
            _fail(f"{where}: exec form only: put the program in `command` and its "
                  f"arguments in `args` (no shell)")
        env = raw.get("env", [])
        if not isinstance(env, list) or not all(isinstance(e, str) for e in env):
            _fail(f"{where}: env must be a list of variable names")
        return Handler("command", on_failure, float(timeout), command=command,
                       args=tuple(args), env=tuple(env))
    url, allow_http = raw.get("url"), raw.get("allow_http", False)
    if not isinstance(url, str) or not url:
        _fail(f"{where}: url is required")
    if not isinstance(allow_http, bool):
        _fail(f"{where}: allow_http must be true or false")
    try:
        harness_net.check_url(url, allow_http)
    except ValueError as e:
        _fail(f"{where}: {e}")
    headers_env = raw.get("headers_env", {})
    if not isinstance(headers_env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers_env.items()):
        _fail(f"{where}: headers_env must map header names to variable names")
    return Handler("http", on_failure, float(timeout), url=url,
                   headers_env=dict(headers_env), allow_http=allow_http)


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")   # last-one-wins would hide a hook
        out[key] = value
    return out


def load(path=CONFIG_FILE):
    """Parse the hooks file. Missing file = no hooks; a malformed one raises, so a
    typo can never silently turn a safety hook off."""
    if not os.path.exists(path):
        return EMPTY
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f, object_pairs_hook=_no_duplicate_keys)
    except (OSError, ValueError) as e:
        _fail(f"{path}: cannot read as JSON: {e}")
    hooks = raw.get("hooks") if isinstance(raw, dict) else None
    if not isinstance(hooks, dict):
        _fail(f"{path}: expected an object with a 'hooks' object")
    if set(raw) - {"hooks"}:
        _fail(f"{path}: unknown top-level key(s) {sorted(set(raw) - {'hooks'})}")
    groups = {}
    for event, items in hooks.items():
        if event not in EVENTS:
            _fail(f"{path}: unknown event {event!r}; choose from {', '.join(EVENTS)}")
        if not isinstance(items, list):
            _fail(f"{path}: {event} must be a list")
        for i, item in enumerate(items):
            where = f"{path}: {event}[{i}]"
            if not isinstance(item, dict) or not isinstance(item.get("hooks"), list):
                _fail(f"{where}: expected an object with a 'hooks' list")
            if set(item) - {"matcher", "hooks"}:
                _fail(f"{where}: unknown key(s) {sorted(set(item) - {'matcher', 'hooks'})} "
                      f"(a mistyped matcher would widen the hook to every tool)")
            matcher = item.get("matcher")
            if matcher is not None:
                try:
                    matcher = re.compile(matcher)
                except (re.error, TypeError) as e:
                    _fail(f"{where}: bad matcher: {e}")
            handlers = [_parse_handler(event, h, f"{where}.hooks[{j}]")
                        for j, h in enumerate(item["hooks"])]
            groups.setdefault(event, []).append(Group(matcher, handlers))
    return HooksConfig(groups)


def looks_like_a_file(arg):
    """`-m policy` names a module and `--fix` is a flag: neither is a file to guard."""
    return (os.path.isfile(arg) or "." in os.path.basename(arg)
            or "/" in arg or chr(92) in arg)


def _script_args(cfg):
    for groups in cfg.groups.values():
        for g in groups:
            for h in g.handlers:
                if h.type == "command":
                    yield from (a for a in (h.command, *h.args)
                                if a and not a.startswith("-") and looks_like_a_file(a))


def canonical(path):
    """The name the OS will really use. Windows maps `x.py.`, `x.py ` and
    `x.py::$DATA` onto x.py (measured: open() created the protected file), so those
    are folded away before comparing; then real path + case folding handle ./, .., 
    8.3 short names and symlinks."""
    p = os.path.normpath(path)
    if sys.platform == "win32":
        p = re.sub(r"::\$data$", "", p, flags=re.I)
        drive, rest = os.path.splitdrive(p)
        parts = [seg if seg in (".", "..") else seg.rstrip(". ") for seg in rest.split(os.sep)]
        p = drive + os.sep.join(parts)
    return os.path.normcase(os.path.realpath(p))


def protected_paths(cfg, config_path=CONFIG_FILE):
    """Files the model must not write: the hooks config and everything a command
    hook is given as a file (its script); paths that do not exist yet count too."""
    return {canonical(p) for p in {config_path, *_script_args(cfg)}}


def protected_names(cfg):
    """Lower-case file names to refuse inside shell commands (best effort)."""
    names = {CONFIG_FILE}
    for a in _script_args(cfg):
        base = os.path.basename(a).lower()
        if "." in base:
            names.add(base)
    return names


def mentions_any(text, names):
    """Whole-name match (not graph.py for h.py) of any file name in a shell string.
    Callers pass text already lower-cased and stripped of ^ and quotes."""
    names = sorted(names, key=len, reverse=True)
    # Windows drops trailing dots, so `h.py.` is h.py: a dot ends the name unless a name
    # character follows it (`h.py.bak` is another file)
    pattern = r"(?<![\w.-])(?:" + "|".join(re.escape(n) for n in names) + r")(?![\w-]|\.\w)"
    return re.search(pattern, text) is not None


def mentions_protected_name(text, cfg):
    return mentions_any(text, protected_names(cfg))


def is_protected(path, cfg):
    return canonical(path) in protected_paths(cfg)


def kill_tree(proc):
    if sys.platform == "win32":                    # terminate() leaves children
        # the system copy by full path: a bare name would be looked up in the current
        # folder first, where the model can plant a taskkill.exe
        taskkill = os.path.join(os.environ.get("SystemRoot", "C:/Windows"),
                                "System32", "taskkill.exe")
        subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()


def resolve_command(command):
    """Where a configured command really lives. A name with a folder part is used as
    given. A bare name is looked up on PATH only: on Windows CreateProcess would try the
    current folder FIRST (measured: a hook.exe copied there ran for command `hook`), and
    the model can create files in that folder. PATH entries that are relative (`.`,
    `bin`) or are the current folder itself are skipped for the same reason. On Windows
    only PATHEXT candidates count (npx ships an extensionless shim that cannot run next
    to npx.cmd); elsewhere the file must be executable. -> a path, or None."""
    if os.path.dirname(command) or os.path.isabs(command):
        return command
    if os.name == "nt":
        pathext = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        names = [command] if os.path.splitext(command)[1].lower() in pathext             else [command + e for e in pathext]
    else:
        names = [command]
    here = os.path.normcase(os.path.realpath(os.getcwd()))
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip().strip(chr(34))
        if not os.path.isabs(folder) or os.path.normcase(os.path.realpath(folder)) == here:
            continue
        for name in names:
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _env(handler):
    env = {k: v for k, v in os.environ.items() if not SCRUB.search(k)}
    for name in handler.env:                       # the config names what it needs
        if name in os.environ:
            env[name] = os.environ[name]
    env["PYTHONUTF8"], env["PYTHONIOENCODING"] = "1", "utf-8"
    return env


def _cap(text):
    if len(text) <= OUTPUT_CAP:
        return text
    return text[:OUTPUT_CAP] + f"\n[truncated: hook output over {OUTPUT_CAP} chars]"


def _run_command(h, payload):
    """-> (exit_code | None, stdout, stderr, failure_reason | None)"""
    # ASCII-escaped JSON: model output can carry lone surrogates, which UTF-8
    # cannot encode; serialise BEFORE starting a process so a failure leaks none
    data = json.dumps(payload).encode("ascii")
    program = resolve_command(h.command)
    if program is None:
        return None, "", "", f"cannot start {h.command!r}: not found on PATH"
    try:
        proc = subprocess.Popen([program, *h.args], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=_env(h), cwd=os.getcwd())
    except OSError as e:
        return None, "", "", f"cannot start {h.command!r}: {e}"
    try:
        out, err = proc.communicate(data, timeout=h.timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate()                         # reap
        return None, "", "", f"timed out after {h.timeout:g}s"
    return (proc.returncode, out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"), None)


def _interpret_text(text):
    """Stdout / response body -> ('ok', context | None) | ('veto', reason) | ('fail', why)."""
    text = text.strip()
    if not text:
        return "ok", None
    if not (text.startswith("{") and text.endswith("}")):
        return "ok", text                          # plain text = context
    try:
        obj = json.loads(text)
    except ValueError as e:
        return "fail", f"output looks like JSON but is not: {e}"
    hso = obj.get("hookSpecificOutput")
    hso = hso if isinstance(hso, dict) else {}
    if hso.get("permissionDecision") == "deny":
        return "veto", str(hso.get("permissionDecisionReason") or obj.get("reason")
                           or "denied by hook")
    if obj.get("decision") == "block":
        return "veto", str(obj.get("reason") or "blocked by hook")
    ctx = hso.get("additionalContext")             # allow/ask/defer are NOT grants
    return "ok", str(ctx) if ctx else None


def _call(h, payload):
    """-> ('ok', context|None) | ('veto', reason) | ('fail', why)"""
    if h.type == "command":
        code, out, err, failure = _run_command(h, payload)
        if failure:
            return "fail", failure
        if code == 2:
            return "veto", (err.strip() or out.strip() or "blocked by hook (exit 2)")
        if code != 0:
            first = (err.strip().splitlines() or [""])[0]
            return "fail", f"exit {code}: {first}"
        return _interpret_text(out)
    headers = {}
    for header, var in h.headers_env.items():      # secret values live in the env only
        if var not in os.environ:
            return "fail", f"environment variable {var} (header {header}) is not set"
        headers[header] = os.environ[var]
    try:
        status, text = harness_net.post_json(h.url, payload, headers, h.timeout,
                                             allow_http=h.allow_http)
    except Exception as e:                         # connection, timeout, TLS ...
        return "fail", f"{type(e).__name__}: {e}"
    if not 200 <= status < 300:
        return "fail", f"HTTP {status}"
    if text.strip() and not text.strip().startswith("{"):
        return "fail", "2xx response body is not a JSON object"
    return _interpret_text(text)


def run(event, payload, cfg):
    """Run the hooks for `event`, in file order; the first veto stops the rest."""
    res = Result()
    name = payload.get("tool_name") or ""
    for group in cfg.groups.get(event, []):
        if (event in TOOL_EVENTS and group.matcher is not None
                and not group.matcher.fullmatch(name)):
            continue
        for h in group.handlers:
            try:
                kind, val = _call(h, payload)
            except Exception as e:                 # a hook problem must not kill the loop
                kind, val = "fail", f"internal error: {type(e).__name__}: {e}"
            if kind == "ok":
                if val:
                    res.context.append(_cap(val))
            elif kind == "veto":
                if event in PRE_EVENTS:
                    res.blocked, res.reason = True, _cap(val)
                    return res
                if event == "PostToolUse":         # the tool already ran: tell the model
                    res.context.append(_cap(val))
            elif event in PRE_EVENTS:              # a failed hook
                if h.on_failure == "block":
                    res.blocked, res.reason = True, f"hook failed ({val})"
                    return res
                if h.on_failure == "ask":
                    res.failures.append(f"{h.describe()}: {val}")
    return res
