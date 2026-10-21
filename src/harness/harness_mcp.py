"""MCP client: tools from other processes or services (book ch 11).

Transports: stdio (a subprocess, newline-delimited JSON-RPC) and Streamable HTTP.
Eras (spec 2026-07-28 "Versioning and Compatibility"):
  modern  stateless, protocol version + client info in every request's _meta,
          no handshake; HTTP adds MCP-Protocol-Version / Mcp-Method / Mcp-Name headers
  legacy  `initialize` handshake (2025-11-25 and earlier), Mcp-Session-Id on HTTP
The era is detected per server: probe with a modern request; a recognised modern error
means "modern server" (never fall back to the handshake); any other error or silence
means "legacy". Only tools are used (tools/list, tools/call); resources, prompts,
sampling, elicitation, subscriptions, tasks and OAuth are not implemented.

A server is code (or a service) wired into the agent's body, and its tool descriptions
and results are text from a stranger. So: names and descriptions are sanitised, results
become plain text (no binary), calls ask the human by default, the config and the
scripts it runs are protected from the model, and nothing here talks to a URL that
harness_net would refuse.
"""
import base64
import dataclasses
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request

import harness_hooks
import harness_net

CONFIG_FILE = ".harness.mcp.json"
MODERN = "2026-07-28"
LEGACY_PREFERRED = "2025-11-25"
LEGACY_ACCEPTED = ("2025-11-25", "2025-06-18", "2025-03-26")   # tool shapes: only 2025-11-25 read
META = "io.modelcontextprotocol/"
CLIENT_INFO = {"name": "roy_harness", "version": "0.3"}
MODERN_ERROR_CODES = (-32022, -32020)          # UnsupportedProtocolVersion, HeaderMismatch
PROBE_TIMEOUT = 10.0                           # seconds to wait for the era probe on stdio
DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0
MAX_DESCRIPTION = 1000
MAX_PAGES = 20
# Tool definitions travel with EVERY model request, before any approval or hook applies,
# so a server must not be able to flood them. These limits are my own numbers, not the spec's.
MAX_TOOLS_PER_SERVER = 128
MAX_TOOL_SCHEMA_BYTES = 20_000            # one serialised tool definition
MAX_CATALOG_BYTES = 100_000               # all servers together
MAX_LINE = 1_000_000                           # one JSON-RPC message, bytes
NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")  # no underscore: `server__tool` stays unambiguous
SERVER_KEYS = {"stdio": {"type", "command", "args", "env", "timeout", "trust"},
               "http": {"type", "url", "headers_env", "allow_http", "timeout", "trust"}}
PRIMITIVE_TYPES = ("string", "integer", "boolean")
TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class McpConfigError(ValueError):
    pass


class McpError(Exception):
    pass


class McpTimeoutError(McpError):
    pass


class McpRpcError(McpError):
    def __init__(self, code, message, data=None, http_status=None):
        super().__init__(f"{message} (code {code})")
        self.code, self.data, self.http_status = code, data, http_status


class McpHttpError(McpError):
    def __init__(self, status, text=""):
        note = " (redirects are refused)" if 300 <= status < 400 else ""
        super().__init__(f"HTTP {status}{note} {text[:120]}".strip())
        self.status = status


# --------------------------------------------------------------------- config
@dataclasses.dataclass
class Spec:
    type: str
    trust: str = "ask"
    timeout: float = DEFAULT_TIMEOUT
    command: str = ""
    args: tuple = ()
    env: tuple = ()
    url: str = ""
    headers_env: dict = dataclasses.field(default_factory=dict)
    allow_http: bool = False


class McpConfig:
    def __init__(self, servers):
        self.servers = servers          # name -> Spec

    def is_empty(self):
        return not self.servers


def _fail(msg):
    raise McpConfigError(msg)


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _parse_server(name, raw, where):
    if not NAME_RE.match(name):
        _fail(f"{where}: server name must be 1-32 of letters, digits and '-' (no underscore)")
    if not isinstance(raw, dict):
        _fail(f"{where}: a server must be an object")
    kind = raw.get("type")
    if kind not in SERVER_KEYS:
        _fail(f"{where}: type must be 'stdio' or 'http', got {kind!r}")
    unknown = set(raw) - SERVER_KEYS[kind]
    if unknown:
        _fail(f"{where}: unknown key(s) {sorted(unknown)} for a {kind} server")
    trust = raw.get("trust", "ask")
    if trust not in ("ask", "always"):
        _fail(f"{where}: trust must be 'ask' or 'always'")
    timeout = raw.get("timeout", DEFAULT_TIMEOUT)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT):
        _fail(f"{where}: timeout must be a number of seconds, over 0 and at most {MAX_TIMEOUT:g}")
    if kind == "stdio":
        command, args, env = raw.get("command"), raw.get("args", []), raw.get("env", [])
        if not isinstance(command, str) or not command.strip():
            _fail(f"{where}: command is required")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            _fail(f"{where}: args must be a list of strings")
        if not args and len(command.split()) > 1:
            _fail(f"{where}: exec form only: program in `command`, arguments in `args`")
        if not isinstance(env, list) or not all(isinstance(e, str) for e in env):
            _fail(f"{where}: env must be a list of variable names")
        return Spec("stdio", trust, float(timeout), command=command, args=tuple(args),
                    env=tuple(env))
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
    return Spec("http", trust, float(timeout), url=url, headers_env=dict(headers_env),
                allow_http=allow_http)


def load(path=CONFIG_FILE):
    """Parse the MCP config. Missing file = no servers; a malformed one raises."""
    if not os.path.exists(path):
        return McpConfig({})
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f, object_pairs_hook=_no_duplicate_keys)
    except (OSError, ValueError) as e:
        _fail(f"{path}: cannot read as JSON: {e}")
    if not isinstance(raw, dict) or not isinstance(raw.get("servers"), dict):
        _fail(f"{path}: expected an object with a 'servers' object")
    if set(raw) - {"servers"}:
        _fail(f"{path}: unknown top-level key(s) {sorted(set(raw) - {'servers'})}")
    return McpConfig({name: _parse_server(name, spec, f"{path}: servers.{name}")
                      for name, spec in raw["servers"].items()})


# ----------------------------------------------------- sanitising what we show
def sanitize_name(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def clean_text(text, limit):
    """Strip control and format characters (keep newline and tab), cap the length."""
    if not isinstance(text, str):
        return ""
    kept = "".join(c for c in text
                   if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf", "Cs"))
    return kept if len(kept) <= limit else kept[:limit] + "…"


def flatten_result(result):
    """tools/call result -> plain text for the model. Binary never enters the array."""
    if result.get("resultType") == "input_required":
        return ("ERROR: the server asked for user input (elicitation or sampling), "
                "which is not supported by this harness")
    parts = []
    for item in result.get("content") or []:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} omitted: {item.get('mimeType', 'unknown type')}]")
        elif kind == "resource_link":
            parts.append(f"[resource_link {item.get('uri', '?')} ({item.get('name', '')})]")
        elif kind == "resource":
            res = item.get("resource") or {}
            parts.append(str(res["text"]) if "text" in res else
                         f"[embedded resource {res.get('uri', '?')} omitted]")
        else:
            parts.append(f"[unsupported content type {kind!r}]")
    text = "\n".join(parts)
    if not text and "structuredContent" in result:
        text = json.dumps(result["structuredContent"], ensure_ascii=False)
    return ("ERROR (tool reported): " + text) if result.get("isError") else text


# ----------------------------------------------- x-mcp-header (modern HTTP only)
def header_value(value):
    """Spec encoding: plain visible ASCII as is, anything else as =?base64?...?="""
    s = str(value)
    plain = (all(0x20 <= ord(c) <= 0x7E or c == "\t" for c in s) and s == s.strip()
             and not (s.startswith("=?base64?") and s.endswith("?=")))
    if plain:
        return s
    return "=?base64?" + base64.b64encode(s.encode("utf-8")).decode("ascii") + "?="


def _annotated(schema, path, out, reachable):
    """Collect every `x-mcp-header` in a schema; mark those reachable by `properties` only."""
    if isinstance(schema, dict):
        if "x-mcp-header" in schema:
            out.append((path if reachable else None, schema))
        for key, sub in schema.items():
            if key == "properties" and isinstance(sub, dict):
                for prop, node in sub.items():
                    _annotated(node, path + (prop,), out, reachable)
            elif key != "x-mcp-header":
                _annotated(sub, path, out, False)
    elif isinstance(schema, list):
        for sub in schema:
            _annotated(sub, path, out, False)


def mirrored_params(schema):
    """-> [(property path, header suffix)] or raise ValueError (spec: reject the tool)."""
    found = []
    _annotated(schema, (), found, True)
    seen, result = set(), []
    for path, node in found:
        name = node.get("x-mcp-header")
        if path is None:
            raise ValueError("x-mcp-header on a property that is not statically reachable")
        if not isinstance(name, str) or not name or not TOKEN_RE.match(name):
            raise ValueError(f"invalid x-mcp-header value {name!r}")
        if name.lower() in seen:
            raise ValueError(f"duplicate x-mcp-header {name!r}")
        seen.add(name.lower())
        if node.get("type") not in PRIMITIVE_TYPES:
            raise ValueError("x-mcp-header needs a string, integer or boolean property")
        result.append((path, name))
    return result


def _dig(arguments, path):
    value = arguments
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


# -------------------------------------------------------------------- servers
class _Server:
    def __init__(self, name, spec):
        self.name, self.spec = name, spec
        self.era = None                 # "modern" | "legacy"
        self.version = None
        self.warnings = []
        self._ids = 0
        self._id_lock = threading.Lock()
        self._lock = threading.Lock()   # one conversation at a time per server
        self._tools_seen = {}

    def next_id(self):
        with self._id_lock:
            self._ids += 1
            return self._ids

    # --- pieces the transports provide
    def raw_request(self, method, params, timeout):
        raise NotImplementedError

    def raw_notify(self, method, params):
        raise NotImplementedError

    def close(self):
        pass

    # --- shared protocol logic
    def _modern_params(self, params):
        meta = {META + "protocolVersion": MODERN, META + "clientInfo": CLIENT_INFO,
                META + "clientCapabilities": {}}
        return {**params, "_meta": meta}

    def request(self, method, params, timeout=None):
        timeout = timeout or self.spec.timeout
        if self.era == "modern":
            params = self._modern_params(params)
        result = self.raw_request(method, params, timeout)
        if not isinstance(result, dict):
            raise McpError(f"{method}: result is not an object")
        return result

    def _legacy_handshake(self, version):
        result = self.raw_request("initialize", {
            "protocolVersion": version, "capabilities": {},
            "clientInfo": CLIENT_INFO}, self.spec.timeout)
        answered = result.get("protocolVersion")
        if answered not in LEGACY_ACCEPTED:
            raise McpError(f"server answered with protocol version {answered!r}; "
                           f"this client speaks {', '.join(LEGACY_ACCEPTED)}")
        if answered != version:
            self.warnings.append(f"{self.name}: server answered with {answered}, not the "
                                 f"requested {version} (tool shapes unverified for it)")
        self.era, self.version = "legacy", answered
        self._after_initialize()
        self.raw_notify("notifications/initialized", {})

    def _after_initialize(self):
        pass

    def _choose_legacy(self, supported):
        """A modern error listed the versions the server supports: pick one we speak."""
        mutual = [v for v in (supported if isinstance(supported, list) else [])
                  if v in LEGACY_ACCEPTED]
        if not mutual:
            raise McpError(f"no protocol version in common (server supports "
                           f"{supported!r}; this client speaks {MODERN} and "
                           f"{', '.join(LEGACY_ACCEPTED)})")
        return LEGACY_PREFERRED if LEGACY_PREFERRED in mutual else mutual[0]

    def detect_era(self):
        self.era = "modern"
        try:
            self.request("server/discover", {}, self._probe_timeout())
            self.version = MODERN
            return
        except McpRpcError as e:
            # spec: over HTTP the body of a 400 is inspected; the same code behind any
            # other status (a proxy, a broken gateway) is not a modern server speaking
            if e.code in MODERN_ERROR_CODES and e.http_status in (None, 400):
                supported = (e.data or {}).get("supported") if isinstance(e.data, dict) else None
                if isinstance(supported, list) and MODERN in supported:
                    raise McpError(f"modern server rejected {MODERN}: {e}")
                self.era = None
                self._legacy_handshake(self._choose_legacy(supported))
                return
        except McpHttpError:
            pass                         # not a recognised modern error: legacy
        except McpTimeoutError:
            pass                         # silence: legacy
        self.era = None
        self._legacy_handshake(LEGACY_PREFERRED)

    def _probe_timeout(self):
        return self.spec.timeout

    def list_tools(self):
        tools, cursor = [], None
        for _ in range(MAX_PAGES):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict)]
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise McpError(f"tools/list did not finish after {MAX_PAGES} pages")

    def accept_tool(self, tool):
        """-> None if usable, else the reason to skip it (subclasses add rules)."""
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return "inputSchema is not an object schema"
        return None

    def call(self, tool, arguments, timeout=None):
        with self._lock:
            self.before_call()
            result = self.request("tools/call", {"name": tool, "arguments": arguments or {}},
                                  timeout)
            return flatten_result(result)

    def before_call(self):
        pass


class StdioServer(_Server):
    def __init__(self, name, spec):
        super().__init__(name, spec)
        self.proc = None
        self._pending = {}
        self._dead = True
        self._stderr_tail = bytearray()
        self._closed = False

    def _env(self):
        env = {k: v for k, v in os.environ.items() if not harness_hooks.SCRUB.search(k)}
        for var in self.spec.env:
            if var in os.environ:
                env[var] = os.environ[var]
        env["PYTHONUTF8"], env["PYTHONIOENCODING"] = "1", "utf-8"
        return env

    def start(self):
        try:
            self.proc = subprocess.Popen(
                [self.spec.command, *self.spec.args], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env(),
                cwd=os.getcwd())
        except OSError as e:
            raise McpError(f"cannot start {self.spec.command!r}: {e}")
        self._dead, self._pending = False, {}
        threading.Thread(target=self._read_out, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(self.proc,), daemon=True).start()
        try:
            self.detect_era()
        except McpError:
            self._terminate()
            raise

    def _probe_timeout(self):
        return PROBE_TIMEOUT

    def _read_out(self, proc):
        reason = "server exited"
        while True:
            line = proc.stdout.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                reason = "server message too large"
                _kill(proc)
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue                          # tolerate stray output; never obey it
            mid = msg.get("id") if isinstance(msg, dict) else None
            if mid is not None and ("result" in msg or "error" in msg):
                box = self._pending.get(mid)
                if box is not None:
                    box.put(msg)
        self._dead = True
        for box in list(self._pending.values()):
            box.put(reason)

    def _read_err(self, proc):
        while True:
            chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else proc.stderr.read(4096)
            if not chunk:
                return
            self._stderr_tail += chunk
            del self._stderr_tail[:-4096]

    def _send(self, obj):
        if self._dead or self.proc is None or self.proc.stdin is None:
            raise McpError("server is not running")
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError as e:
            raise McpError(f"cannot write to the server: {e}")

    def raw_request(self, method, params, timeout):
        rid = self.next_id()
        box = queue.Queue()
        self._pending[rid] = box
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            try:
                msg = box.get(timeout=timeout)
            except queue.Empty:
                try:
                    self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                "params": {"requestId": rid, "reason": "timeout"}})
                except McpError:
                    pass
                raise McpTimeoutError(f"{method} timed out after {timeout:g}s")
            if isinstance(msg, str):
                tail = self._stderr_tail.decode("utf-8", "replace").strip()[-200:]
                raise McpError(msg + (f" (stderr: {tail})" if tail else ""))
            if "error" in msg:
                e = msg["error"] if isinstance(msg["error"], dict) else {}
                raise McpRpcError(e.get("code"), str(e.get("message", "error")), e.get("data"))
            return msg.get("result")
        finally:
            self._pending.pop(rid, None)

    def raw_notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def before_call(self):
        if self._dead and not self._closed:      # the spec says a client should restart it
            self._terminate()
            self.era = None
            self.start()

    def _terminate(self):
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            harness_hooks.kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        self._dead = True

    def close(self):
        self._closed = True
        self._terminate()


def _kill(proc):
    try:
        harness_hooks.kill_tree(proc)
    except Exception:
        pass


class HttpServer(_Server):
    def __init__(self, name, spec):
        super().__init__(name, spec)
        self.session = None
        self._mirror = {}

    def start(self):
        self._check_env()
        self.detect_era()

    def _check_env(self):
        for header, var in self.spec.headers_env.items():
            if var not in os.environ:
                raise McpError(f"environment variable {var} (header {header}) is not set")

    def _after_initialize(self):
        pass

    def accept_tool(self, tool):
        reason = super().accept_tool(tool)
        if reason or self.era != "modern":
            return reason
        try:                                       # spec: reject, and exclude only this tool
            self._mirror[tool["name"]] = mirrored_params(tool["inputSchema"])
        except ValueError as e:
            return str(e)
        return None

    def _headers(self, method, params):
        self._check_env()
        h = {"content-type": "application/json",
             "accept": "application/json, text/event-stream",
             "user-agent": "harness101-mcp/0.3"}
        for header, var in self.spec.headers_env.items():
            h[header] = os.environ[var]
        if self.era == "modern":
            h["MCP-Protocol-Version"] = MODERN
            h["Mcp-Method"] = method
            if method == "tools/call":
                name = params.get("name", "")
                h["Mcp-Name"] = header_value(name)
                for path, suffix in self._mirror.get(name, []):
                    value = _dig(params.get("arguments") or {}, path)
                    if value is not None:
                        if isinstance(value, bool):
                            value = "true" if value else "false"
                        h["Mcp-Param-" + suffix] = header_value(value)
        else:
            if self.session:
                h["Mcp-Session-Id"] = self.session
            if self.version:
                h["MCP-Protocol-Version"] = self.version
        return h

    def _exchange(self, body, headers, timeout, want_id):
        """-> (status, response headers, message dict | None, text). Bounded reads."""
        req = urllib.request.Request(self.spec.url, data=json.dumps(body).encode("ascii"),
                                     headers=headers, method="POST")
        harness_net.check_url(self.spec.url, self.spec.allow_http)
        try:
            resp = harness_net.open_url(req, timeout)
        except urllib.error.HTTPError as e:
            raw = e.read(harness_net.MAX_RESPONSE + 1)    # the whole body, bounded: a cut
            text = raw[:2000].decode("utf-8", "replace")  # JSON error would not parse
            try:                          # a recognised modern error arrives as a JSON body
                msg = json.loads(raw) if len(raw) <= harness_net.MAX_RESPONSE else None
            except ValueError:
                msg = None
            return e.code, {k.lower(): v for k, v in e.headers.items()}, msg, text
        except (urllib.error.URLError, OSError) as e:
            if "timed out" in str(e).lower():
                raise McpTimeoutError(f"{body.get('method')} timed out after {timeout:g}s")
            raise McpError(f"{type(e).__name__}: {e}")
        with resp:
            rheaders = {k.lower(): v for k, v in resp.headers.items()}
            ctype = rheaders.get("content-type", "")
            if "text/event-stream" in ctype:
                return resp.status, rheaders, self._read_sse(resp, want_id), ""
            raw = resp.read(harness_net.MAX_RESPONSE + 1)
            if len(raw) > harness_net.MAX_RESPONSE:
                raise McpError(f"response too large (over {harness_net.MAX_RESPONSE} bytes)")
            text = raw.decode("utf-8", "replace")
            if not text.strip():
                return resp.status, rheaders, None, text
            try:
                return resp.status, rheaders, json.loads(text), text
            except ValueError:
                raise McpError("response is not JSON")

    def _read_sse(self, resp, want_id):
        total, data = 0, []
        while True:
            line = resp.readline(MAX_LINE + 1)
            total += len(line)
            if total > harness_net.MAX_RESPONSE * 4 or len(line) > MAX_LINE:
                raise McpError("response too large (event stream)")
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text.startswith("data:"):
                data.append(text[5:].lstrip())
            elif text == "" and data:
                try:
                    msg = json.loads("\n".join(data))
                except ValueError:
                    msg = None
                data = []
                if isinstance(msg, dict) and msg.get("id") == want_id and (
                        "result" in msg or "error" in msg):
                    return msg
        raise McpError("event stream ended without a response")

    def raw_request(self, method, params, timeout):
        rid = self.next_id()
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        status, rheaders, msg, text = self._exchange(
            body, self._headers(method, params), timeout, rid)
        if method == "initialize" and "mcp-session-id" in rheaders:
            self.session = rheaders["mcp-session-id"]
        if not 200 <= status < 300:
            err = msg.get("error") if isinstance(msg, dict) else None
            if isinstance(err, dict):
                raise McpRpcError(err.get("code"), str(err.get("message", "error")),
                                  err.get("data"), http_status=status)
            raise McpHttpError(status, text)
        if not isinstance(msg, dict):
            raise McpError(f"{method}: empty response")
        if "error" in msg:
            e = msg["error"] if isinstance(msg["error"], dict) else {}
            raise McpRpcError(e.get("code"), str(e.get("message", "error")), e.get("data"),
                              http_status=status)
        return msg.get("result")

    def raw_notify(self, method, params):
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        status, _, _, text = self._exchange(body, self._headers(method, params),
                                            self.spec.timeout, None)
        if not 200 <= status < 300:
            raise McpHttpError(status, text)


# ------------------------------------------------------------------- registry
class Registry:
    def __init__(self, cfg=None):
        self.cfg = cfg or McpConfig({})
        self.servers, self.failures, self.warnings = {}, {}, []
        self._tools = {}            # exposed name -> (server, tool name, definition)
        self._catalog_bytes = 0     # serialised size of everything in _tools

    def is_empty(self):
        return not self.servers

    def add(self, server):
        raw = server.list_tools()
        added = 0
        for tool in raw:
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                self.warnings.append(f"{server.name}: skipped a tool without a name")
                continue
            reason = server.accept_tool(tool)
            if reason:
                self.warnings.append(f"{server.name}: skipped tool {name!r}: {reason}")
                continue
            exposed = f"{server.name}__{sanitize_name(name)}"
            if exposed in self._tools:
                self.warnings.append(f"{server.name}: skipped tool {name!r}: its name "
                                     f"collides with {exposed!r}")
                continue
            description = clean_text(tool.get("description"), MAX_DESCRIPTION)
            definition = {"name": exposed,
                          "description": f"[mcp:{server.name}] {description}".strip(),
                          "input_schema": tool["inputSchema"]}
            size = len(json.dumps(definition))
            if size > MAX_TOOL_SCHEMA_BYTES:
                self.warnings.append(f"{server.name}: skipped tool {name!r}: its definition is "
                                     f"too large ({size} bytes, limit {MAX_TOOL_SCHEMA_BYTES})")
                continue
            if added >= MAX_TOOLS_PER_SERVER:
                self.warnings.append(f"{server.name}: stopped at the limit of "
                                     f"{MAX_TOOLS_PER_SERVER} tools per server; the rest are ignored")
                break
            if self._catalog_bytes + size > MAX_CATALOG_BYTES:
                self.warnings.append(f"{server.name}: stopped, the tool catalog budget of "
                                     f"{MAX_CATALOG_BYTES} bytes is used up; the rest are ignored")
                break
            self._catalog_bytes += size
            self._tools[exposed] = (server, name, definition)
            added += 1
        server.tool_count = added
        self.servers[server.name] = server
        self.warnings += server.warnings

    def tools(self):
        return [definition for _, _, definition in self._tools.values()]

    def owns(self, name):
        return name in self._tools

    def server_of(self, name):
        return self._tools[name][0].name

    def tool_of(self, name):
        return self._tools[name][1]

    def trust_of(self, name):
        return self._tools[name][0].spec.trust

    def call(self, name, args):
        if name not in self._tools:
            return f"ERROR: unknown MCP tool {name}"
        server, tool, _ = self._tools[name]
        try:
            return server.call(tool, args if isinstance(args, dict) else {})
        except McpError as e:
            return f"ERROR: MCP {server.name}.{tool}: {e}"
        except Exception as e:                       # never let a server crash the loop
            return f"ERROR: MCP {server.name}.{tool}: {type(e).__name__}: {e}"

    def describe(self):
        lines = [f"{n}: {s.spec.type} {s.era}, {getattr(s, 'tool_count', 0)} tools "
                 f"(trust={s.spec.trust})" for n, s in self.servers.items()]
        lines += [f"{n}: FAILED {why}" for n, why in self.failures.items()]
        return lines

    def close(self):
        for server in self.servers.values():
            try:
                server.close()
            except Exception:
                pass


EMPTY = Registry()


def connect_all(cfg):
    """Start every configured server; one that fails is reported and skipped."""
    reg = Registry(cfg)
    for name, spec in cfg.servers.items():
        server = (StdioServer if spec.type == "stdio" else HttpServer)(name, spec)
        try:
            server.start()
            reg.add(server)
        except McpError as e:
            reg.failures[name] = str(e)
            server.close()
        except Exception as e:
            reg.failures[name] = f"{type(e).__name__}: {e}"
            server.close()
    return reg


# ------------------------------------------------- what the model must not edit
def _spec_files(cfg):
    for spec in cfg.servers.values():
        if spec.type == "stdio":
            yield from (a for a in (spec.command, *spec.args)
                        if a and not a.startswith("-") and harness_hooks.looks_like_a_file(a))


def protected_paths(reg):
    return {harness_hooks.canonical(p) for p in {CONFIG_FILE, *_spec_files(reg.cfg)}}


def protected_names(reg):
    names = {CONFIG_FILE}
    for a in _spec_files(reg.cfg):
        base = os.path.basename(a).lower()
        if "." in base:
            names.add(base)
    return names


def is_protected(path, reg):
    return harness_hooks.canonical(path) in protected_paths(reg)


def mentions_protected_name(text, reg):
    return harness_hooks.mentions_any(text, protected_names(reg))
