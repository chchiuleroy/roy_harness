"""kb doctor (Gate 2 OPT6): which optional capabilities are present, what a missing one costs, and whether the data folder is healthy.

Nothing here has a hard-coded install location: executables are found through environment variables (KB_LLAMA_SERVER,
KB_EMBED_MODEL, KB_WHISPER) or PATH. A missing capability is a report line, never an exception.
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass

from . import cas, commit, db
from .errors import ArtifactsMissing


@dataclass(frozen=True)
class Capability:
    name: str
    available: bool
    detail: str
    affects: tuple


def check_capabilities(which=shutil.which, find_spec=importlib.util.find_spec, environ=None):
    env = os.environ if environ is None else environ

    def exe(var, *names):
        p = env.get(var)
        if p and os.path.isfile(p):
            return p
        for n in names:
            found = which(n)
            if found:
                return found
        return None

    try:
        probe = sqlite3.connect(":memory:")
        try:
            fts5 = any("FTS5" in r[0] for r in probe.execute("PRAGMA compile_options"))
        finally:
            probe.close()
    except sqlite3.Error:
        fts5 = False
    llama = exe("KB_LLAMA_SERVER", "llama-server")
    model = env.get("KB_EMBED_MODEL")
    whisper = exe("KB_WHISPER", "whisper-cli", "whisper")
    return [
        Capability("sqlite_fts5", fts5, f"sqlite {sqlite3.sqlite_version}", ("all text search",)),
        Capability("python", sys.version_info >= (3, 11), sys.version.split()[0], ("everything",)),
        Capability("numpy", find_spec("numpy") is not None, "needed for the dense tier", ("dense retrieval",)),
        Capability("embedding_server", bool(llama and model and os.path.isfile(model)),
                   f"server={llama or 'not found (set KB_LLAMA_SERVER)'}; model={model or 'not set (KB_EMBED_MODEL)'}",
                   ("dense retrieval",)),
        Capability("pdf_pymupdf", find_spec("fitz") is not None, "PyMuPDF", ("pdf",)),
        Capability("ffmpeg", which("ffmpeg") is not None, which("ffmpeg") or "not found on PATH", ("audio", "video")),
        Capability("speech_recognition", bool(whisper) or find_spec("faster_whisper") is not None,
                   whisper or ("faster_whisper module" if find_spec("faster_whisper") else "not found (KB_WHISPER)"),
                   ("audio", "video")),
        Capability("ocr_windows", sys.platform == "win32", "operating-system component; language packs are not checked here",
                   ("image", "screenshot")),
    ]


def check_home(home, deep=False):
    """Returns a list of problems (empty means healthy). deep re-hashes every stored blob."""
    problems = []
    if not os.path.isfile(home.db_path):
        return [("db_missing", f"{home.db_path} does not exist; run kb init")]
    conn = db.connect(home.db_path, readonly=True)
    try:
        v = db.schema_version(conn)
        if v != db.MIGRATIONS[-1].version:
            problems.append(("schema_version", f"database is schema {v}, this kb expects {db.MIGRATIONS[-1].version}"))
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            problems.append(("integrity", "SQLite integrity_check failed"))
        tmp = os.path.join(home.sub("blobs"), "_tmp")
        if os.path.isdir(tmp) and os.listdir(tmp):
            problems.append(("leftover_temp", f"{len(os.listdir(tmp))} unfinished blob temp file(s); a writer start will clean them"))
        if v == db.MIGRATIONS[-1].version:
            active = commit.active_commit_id(conn)
            if active:
                try:
                    commit.active_artifacts(home, conn)
                except ArtifactsMissing as e:
                    problems.append(("active_commit_artifacts", str(e)))
                else:
                    if deep:
                        manifest, bad = commit.verify_artifacts(commit.segment_dir(home, active))
                        problems.extend(("active_commit_damaged", b) for b in bad)
                        if manifest is not None and not bad:
                            problems.extend(("ghost_check", g) for g in commit.ghost_check(conn, active, commit.segment_dir(home, active), manifest))
            live = {r[0] for r in conn.execute("SELECT commit_id FROM commits WHERE state IN ('staging','verified','publishing')")}
            stale = [n for n in os.listdir(home.sub("staging")) if n not in live] if os.path.isdir(home.sub("staging")) else []
            if stale:
                problems.append(("leftover_staging", f"{len(stale)} staging folder(s) belong to no running commit; `kb recover` removes them"))
        if deep and v == db.MIGRATIONS[-1].version:
            store = cas.BlobStore(home.sub("blobs"))
            for (sha,) in conn.execute("SELECT sha256 FROM blobs"):
                state = store.verify(conn, sha)
                if state != cas.OK:
                    problems.append(("blob_" + state, sha))
    finally:
        conn.close()
    return problems


def embedding_backend_capability(home):
    """The embedding backend this data folder has pinned (embedder.json), read as plain JSON: this module is tier T0 and may not import the
    embedder. The pinned files must still exist; the model hash is checked when the server starts, not here."""
    path = os.path.join(home.path, "embedder.json")
    if not os.path.isfile(path):
        return Capability("embedding_backend", False, "none pinned (kb embedder set --server ... --model ...)", ("dense retrieval",))
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        missing = [k for k in ("server", "model") if not os.path.isfile(cfg[k])]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return Capability("embedding_backend", False, f"embedder.json is unreadable ({e})", ("dense retrieval",))
    if missing:
        return Capability("embedding_backend", False, f"pinned {' and '.join(missing)} file not found", ("dense retrieval",))
    return Capability("embedding_backend", True, f"{cfg.get('model_id')} (pinned, dimension {cfg.get('dim') or 'not known yet'})", ("dense retrieval",))


def report(home, deep=False):
    caps = check_capabilities() + [embedding_backend_capability(home)]
    problems = check_home(home, deep=deep)
    return {"capabilities": [asdict(c) for c in caps], "problems": [list(p) for p in problems], "healthy": not problems}
