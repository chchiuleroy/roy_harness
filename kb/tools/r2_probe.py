"""R2 probe: what does importing the harness do, and does having kb installed change any of it? (Gate 2 OPT2)
Usage: python tools/r2_probe.py <harness source dir> <working dir>  -> prints one JSON object.
Recorded during the import: loaded module set, every subprocess started, every socket bound or listened on, every file opened;
afterwards: hash of the system prompt, hash of the main agent's tool schema, and the MCP config the harness would load from
the working directory. Timing is reported separately and is NOT part of the equality comparison (see tools/measure_o2.py).
"""
import builtins
import hashlib
import json
import os
import socket
import subprocess
import sys
import time

harness_dir, workdir = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
os.chdir(workdir)
sys.path.insert(0, harness_dir)

import signal
import threading

rec = {"popen": [], "bind": [], "listen": 0, "opens": set(), "connect": []}
env_before = dict(os.environ)
signals_before = {s: repr(signal.getsignal(s)) for s in (signal.SIGINT, signal.SIGTERM)}
threads_before = {t.name for t in threading.enumerate()}
_open = builtins.open
_popen_init = subprocess.Popen.__init__
_bind, _listen = socket.socket.bind, socket.socket.listen
_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex


def open_spy(file, *a, **k):
    if isinstance(file, (str, bytes, os.PathLike)):
        rec["opens"].add(os.path.abspath(os.fsdecode(file)).replace(os.sep, "/").lower())
    return _open(file, *a, **k)


def popen_spy(self, args, *a, **k):
    rec["popen"].append(args if isinstance(args, str) else " ".join(map(str, args)))
    return _popen_init(self, args, *a, **k)


builtins.open = open_spy
subprocess.Popen.__init__ = popen_spy
socket.socket.bind = lambda self, addr: (rec["bind"].append(str(addr)), _bind(self, addr))[1]
socket.socket.listen = lambda self, *a: (rec.__setitem__("listen", rec["listen"] + 1), _listen(self, *a))[1]
socket.socket.connect = lambda self, addr: (rec["connect"].append(str(addr)), _connect(self, addr))[1]
socket.socket.connect_ex = lambda self, addr: (rec["connect"].append(str(addr)), _connect_ex(self, addr))[1]

before = set(sys.modules)
t0 = time.perf_counter()
import harness_vllm  # noqa: E402

import_s = time.perf_counter() - t0
builtins.open = _open
subprocess.Popen.__init__ = _popen_init
socket.socket.bind, socket.socket.listen = _bind, _listen
socket.socket.connect, socket.socket.connect_ex = _connect, _connect_ex

import harness_mcp  # noqa: E402

cfg_path = os.path.join(workdir, ".harness.mcp.json")
cfg_bytes = open(cfg_path, "rb").read() if os.path.exists(cfg_path) else None
tools = json.dumps(harness_vllm.tools_for(0), sort_keys=True, default=str)
out = {
    "modules": sorted(m for m in set(sys.modules) - before if not m.startswith("_")),
    "popen": rec["popen"], "bind": rec["bind"], "listen": rec["listen"], "connect": rec["connect"],
    "new_threads": sorted({t.name for t in threading.enumerate()} - threads_before),
    "env_changed": sorted(k for k in set(os.environ) | set(env_before) if os.environ.get(k) != env_before.get(k)),
    "signal_handlers_changed": sorted(str(s) for s in signals_before if repr(signal.getsignal(s)) != signals_before[s]),
    "opens": sorted(p for p in rec["opens"] if "__pycache__" not in p and not p.endswith(".pyc")),
    "system_prompt_sha256": hashlib.sha256(harness_vllm.system_prompt().encode("utf-8")).hexdigest(),
    "tools_sha256": hashlib.sha256(tools.encode("utf-8")).hexdigest(),
    "mcp_config_sha256": hashlib.sha256(cfg_bytes).hexdigest() if cfg_bytes is not None else None,
    "mcp_servers": sorted(harness_mcp.load(cfg_path).servers),
    "import_s": import_s,
}
print(json.dumps(out))
