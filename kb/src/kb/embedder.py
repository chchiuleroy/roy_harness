"""The embedding backend (Gate 2 section 4.11, stage S3; tier T1: needs numpy and a local llama.cpp server).

One resident `llama-server` process answers embedding requests on 127.0.0.1 only, started with the flags S-1 measured as the small
configuration (one slot, 4096 tokens: about 0.8 GB resident, 41 to 53 ms median per query, versus 9.4 GB with the default four slots).
It starts on the first use, outlives the command that started it, and `stop()` ends it.

The model is not discovered, it is PINNED: `pin()` hashes the model file once and records the hash with the server path in
`<home>/embedder.json`; every start re-checks the hash (T-X1: a replaced model file refuses to run). The identity of an index's vectors is
(model id, model hash, dimension, pooling, query instruction): any change means a different index, never a mixed one (F10, T-I2).

kb sends no text to anything but this local process. Nothing is downloaded here: the model file and the server program are ones the person
already has and names.
"""
import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import numpy as np

from .canonical import canonical_bytes
from .durable import fsync_dir
from .errors import KbError, VectorUnavailable

DEFAULT_INSTRUCTION = "Given a search query, retrieve relevant passages that answer the query"
POOLING = "last"
MAX_CHARS = 3000                       # the chunker never makes a chunk longer than 2400 characters; more than this would not fit the 4096-token window
BATCH = 16
SERVER_FLAGS = ["--embeddings", "--pooling", POOLING, "-ngl", "99", "-np", "1", "-c", "4096", "-b", "4096", "-ub", "4096", "--host", "127.0.0.1"]
START_TIMEOUT_S = 120
CONFIG_NAME = "embedder.json"
STATE_DIR = "embedder"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # a system proxy must never see (or break) a localhost call


class EmbedderUnavailable(VectorUnavailable):
    """No embedding backend is configured, pinned correctly and running; vector search is not available (lexical search still is)."""


class EmbedError(VectorUnavailable):
    """The embedding backend refused or mangled a request."""


@dataclass(frozen=True)
class Identity:
    model_id: str
    model_sha256: str
    dim: int
    pooling: str
    instruction: str

    def as_dict(self):
        return {"model_id": self.model_id, "model_sha256": self.model_sha256, "dim": self.dim, "pooling": self.pooling,
                "instruction_sha256": hashlib.sha256(self.instruction.encode("utf-8")).hexdigest()}

    def key(self):
        return hashlib.sha256(canonical_bytes(self.as_dict())).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def config_path(home):
    return os.path.join(home.path, CONFIG_NAME)


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(os.path.dirname(path))


def load_config(home):
    try:
        with open(config_path(home), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise EmbedderUnavailable(f"{CONFIG_NAME} is unreadable ({e}); run kb embedder set again") from None


def pin(home, server, model, instruction=None):
    """Record the server program and the model file and pin the model's hash. This is the approval step: whatever file is here now is the
    one kb will accept later. Existing dimension knowledge is dropped because the model may have changed."""
    for label, path in (("server", server), ("model", model)):
        if not os.path.isfile(path):
            raise EmbedderUnavailable(f"{label} file not found: {path}")
    cfg = {"server": os.path.abspath(server), "model": os.path.abspath(model), "model_sha256": sha256_file(model),
           "model_id": os.path.basename(model), "instruction": instruction or DEFAULT_INSTRUCTION, "dim": None, "verified": None}
    _write_json(config_path(home), cfg)
    return cfg


def _verify_model(home, cfg):
    """The model file must still be the pinned one. The (size, mtime) of a file that passed is remembered so that a start does not hash 600 MB
    again when nothing changed; any change in size or time brings the full hash back."""
    st = os.stat(cfg["model"])
    stamp = [st.st_size, st.st_mtime_ns]
    if cfg.get("verified") == stamp:
        return
    if sha256_file(cfg["model"]) != cfg["model_sha256"]:
        raise EmbedderUnavailable(f"the model file {cfg['model']} does not match the pinned hash; refusing to run it (run kb embedder set to approve a new file)")
    cfg["verified"] = stamp
    _write_json(config_path(home), cfg)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port, path, timeout=2):
    return _OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=timeout).read()


def _healthy(port):
    try:
        _get(port, "/health")
        return True
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _listener_pid(port):
    """The process id that listens on 127.0.0.1:<port>, or None when that cannot be told. A health answer proves that SOMETHING listens there,
    not that it is the process kb recorded (ports and process ids are recycled), so nothing is ended on a guess."""
    try:
        if os.name == "nt":
            out = (subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=20).stdout or b"").decode("ascii", "ignore")   # headers are localized, the numbers are not
            for line in out.splitlines():
                cols = line.split()
                if len(cols) >= 5 and cols[1] == f"127.0.0.1:{port}" and cols[2] == "0.0.0.0:0" and cols[4].isdigit():
                    return int(cols[4])
            return None
        out = (subprocess.run(["lsof", "-nP", f"-iTCP@127.0.0.1:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, timeout=20).stdout or b"").decode("ascii", "ignore").split()
        return int(out[0]) if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class LlamaServerEmbedder:
    """Embeddings from a resident llama-server. `identity` needs the dimension, which is learned from the first real answer and saved."""

    def __init__(self, home, cfg=None):
        self.home = home
        self.cfg = cfg or load_config(home)
        if not self.cfg:
            raise EmbedderUnavailable("no embedding backend is configured; run kb embedder set --server <llama-server> --model <gguf>")
        self.state_path = os.path.join(home.path, STATE_DIR, "server.json")
        self._port = None

    # ------------------------------------------------------------------ process
    def _read_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _fingerprint(self):
        """What a server must have been started with to answer for the current pin (a re-pinned model or other flags make an old server foreign)."""
        return {"server": self.cfg["server"], "model": self.cfg["model"], "model_sha256": self.cfg["model_sha256"], "flags": SERVER_FLAGS}

    def ensure_running(self):
        st = self._read_state()
        if st and _healthy(st["port"]):
            if st.get("fingerprint") == self._fingerprint() and _listener_pid(st["port"]) == st["pid"]:        # the recorded process, not a stranger on a recycled port
                self._port = st["port"]
                return self._port
            self._end(st)                         # a server that was started for another pin must not answer for this one
        return self._start()

    def _start(self):
        _verify_model(self.home, self.cfg)
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        os.makedirs(self.home.sub("logs"), exist_ok=True)
        port = _free_port()
        log_path = self.home.sub("logs") + "/embedder.log"
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
        with open(log_path, "ab") as log:
            proc = subprocess.Popen([self.cfg["server"], "-m", self.cfg["model"], *SERVER_FLAGS, "--port", str(port)],
                                    stdout=subprocess.DEVNULL, stderr=log, creationflags=flags)
        t0 = time.monotonic()
        while time.monotonic() - t0 < START_TIMEOUT_S:
            if proc.poll() is not None:
                raise EmbedderUnavailable(f"the embedding server exited at start (code {proc.returncode}); see {log_path}")
            if _healthy(port):
                break
            time.sleep(0.3)
        else:
            self._kill(proc.pid)
            raise EmbedderUnavailable(f"the embedding server did not become ready in {START_TIMEOUT_S} s; see {log_path}")
        _write_json(self.state_path, {"pid": proc.pid, "port": port, "fingerprint": self._fingerprint(),
                                      "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        self._port = port
        return port

    @staticmethod
    def _kill(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        else:
            try:
                os.kill(pid, 15)
            except OSError:
                pass

    def _end(self, st):
        """End the server recorded in `st`, but only a process that is proved to be the one: it answers on the recorded port AND that port is
        listened on by the recorded process id. Anything else (a recycled port or process id) is left alone. A listener that is the recorded
        process but no longer answers (a hung server) is ended too: the proof of ownership is the listener, not the answer."""
        if _listener_pid(st["port"]) == st["pid"]:
            self._kill(st["pid"])
            return True
        return False

    def stop(self):
        """End the resident server this home started (see `_end`: a server that cannot be proved to be ours is left running)."""
        st = self._read_state()
        stopped = bool(st) and self._end(st)
        try:
            os.remove(self.state_path)
        except OSError:
            pass
        self._port = None
        return stopped

    def status(self):
        st = self._read_state()
        return {"configured": True, "running": bool(st and _healthy(st["port"])), "model": self.cfg["model_id"],
                "pid": st["pid"] if st else None, "port": st["port"] if st else None}

    # ------------------------------------------------------------------ vectors
    def _post(self, texts):
        for t in texts:
            if len(t) > MAX_CHARS:
                raise EmbedError(f"a text of {len(t)} characters is longer than the {MAX_CHARS} this backend accepts")
        port = self._port or self.ensure_running()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/embeddings", data=json.dumps({"input": texts, "model": "kb"}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            body = json.loads(_OPENER.open(req, timeout=600).read())
        except (OSError, urllib.error.URLError, ValueError) as e:
            raise EmbedError(f"the embedding server failed: {e}") from None
        rows = sorted(body.get("data", []), key=lambda d: d["index"])
        if len(rows) != len(texts):
            raise EmbedError(f"asked for {len(texts)} vectors, got {len(rows)}")
        v = np.array([r["embedding"] for r in rows], dtype=np.float32)
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        if not np.isfinite(v).all() or (norms == 0).any():
            raise EmbedError("the embedding server returned a vector that is not finite or is all zeros")
        self._learn_dim(v.shape[1])
        return v / norms

    def _learn_dim(self, dim):
        if self.cfg.get("dim") != dim:
            self.cfg["dim"] = dim
            _write_json(config_path(self.home), self.cfg)

    @property
    def identity(self):
        if not self.cfg.get("dim"):
            self._post(["dimension probe"])
        return Identity(self.cfg["model_id"], self.cfg["model_sha256"], int(self.cfg["dim"]), POOLING, self.cfg["instruction"])

    def embed_documents(self, texts):
        out = [self._post(texts[i:i + BATCH]) for i in range(0, len(texts), BATCH)]
        return np.concatenate(out) if out else np.zeros((0, int(self.cfg.get("dim") or 0)), dtype=np.float32)

    def embed_query(self, text):
        return self._post([f"Instruct: {self.cfg['instruction']} Query:{text}"])[0]
