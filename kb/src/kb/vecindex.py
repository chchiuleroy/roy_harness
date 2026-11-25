"""The vector artifact of a commit and the cache that feeds it (Gate 2 sections 4.10 and 4.11, stage S3; tier T1: needs numpy).

Embedding is the slow step (hours for a big collection), so it is done ahead of the commit by `kb embed` and kept in a CACHE keyed by
(index identity, sha256 of the chunk text): the same text under the same model is embedded once, however many commits or sources contain it. The
commit then only COPIES the vectors of its member chunks into one file, `vec.npy`, rows in the order of the sorted member chunk ids, and records
the identity next to the file hash in the manifest. A commit therefore holds exactly the vectors of exactly its members (both directions are
checked before it is published), and removing text from the commit removes its vectors at the next prune (a vector is derived data, not a copy
of the text, but it is still removed with it).

A chunk without a vector never gets a quiet zero row: up to `max_inline` missing chunks are embedded during the commit, more than that and
the commit stops with the number to embed first (`kb embed`). Nothing is published half way.
"""
import os
import sqlite3

import numpy as np

from . import ids
from .errors import KbError

NAME = "vec.npy"
CACHE_REL = ("vectors", "cache.db")
NORM_TOLERANCE = 1e-3
FORMAT = 1


class NeedEmbedding(KbError):
    """Too many chunks of the commit have no vector yet; run kb embed first (nothing was changed)."""


def signature_of(identity):
    """What a commit records about its vectors and what the running backend must equal for them to be searched (T-I2)."""
    return dict(identity.as_dict(), format=FORMAT, dtype="float32", rows="sorted member chunk ids")


def cache_path(home):
    return os.path.join(home.path, *CACHE_REL)


class VectorCache:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS vec(identity TEXT NOT NULL, text_sha256 TEXT NOT NULL, dim INTEGER NOT NULL, "
                          "vector BLOB NOT NULL, PRIMARY KEY(identity, text_sha256)) WITHOUT ROWID")

    def close(self):
        self.conn.close()

    def get_many(self, identity, shas):
        out = {}
        shas = list(shas)
        for i in range(0, len(shas), 500):
            part = shas[i:i + 500]
            marks = ",".join("?" * len(part))
            for sha, blob in self.conn.execute(f"SELECT text_sha256, vector FROM vec WHERE identity = ? AND text_sha256 IN ({marks})", [identity, *part]):
                out[sha] = np.frombuffer(blob, dtype=np.float32)
        return out

    def put_many(self, identity, vectors):
        """vectors: {text_sha256: float32 array}. One transaction, so an interrupted embedding run keeps every batch that finished."""
        self.conn.execute("BEGIN")
        try:
            for sha, v in vectors.items():
                self.conn.execute("INSERT OR REPLACE INTO vec VALUES (?,?,?,?)", (identity, sha, int(v.shape[0]), np.asarray(v, dtype=np.float32).tobytes()))
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def keep_only(self, identity, shas):
        """Drop every row that is not a vector of `shas` under `identity` (other identities go entirely: a model change is a rebuild)."""
        keep = set(shas)
        stale = [(i, s) for i, s in self.conn.execute("SELECT identity, text_sha256 FROM vec") if i != identity or s not in keep]
        if stale:
            self.conn.execute("BEGIN")
            self.conn.executemany("DELETE FROM vec WHERE identity = ? AND text_sha256 = ?", stale)
            self.conn.execute("COMMIT")
        return len(stale)

    def count(self, identity):
        return self.conn.execute("SELECT COUNT(*) FROM vec WHERE identity = ?", (identity,)).fetchone()[0]


def chunk_shas(asm):
    """{text_sha256: text} of the distinct chunk texts of an assembly."""
    return {ids.text_sha256(c.text): c.text for c in asm.chunks}


def missing(cache, identity, asm):
    texts = chunk_shas(asm)
    have = cache.get_many(identity.key(), texts)
    return {sha: t for sha, t in texts.items() if sha not in have}


def embed_missing(cache, embedder, identity, todo, progress=None, step=64):
    """Embed the texts of `todo` ({sha: text}) and store each batch as soon as it is done. Returns how many were embedded."""
    items = sorted(todo.items())
    done = 0
    for i in range(0, len(items), step):
        part = items[i:i + step]
        vecs = embedder.embed_documents([t for _, t in part])
        cache.put_many(identity.key(), {sha: v for (sha, _), v in zip(part, vecs)})
        done += len(part)
        if progress:
            progress(done, len(items))
    return done


class VectorExtension:
    """The commit-side half: prepare (make sure every vector exists), write (copy them into the artifact), check (compare the artifact with its
    members), finish (prune the cache to what the new commit holds)."""
    name = NAME

    def __init__(self, home, embedder, max_inline=200):
        self.home, self.embedder, self.max_inline = home, embedder, max_inline
        self._identity = None

    @property
    def identity(self):
        if self._identity is None:
            self._identity = self.embedder.identity
        return self._identity

    def signature(self):
        return signature_of(self.identity)

    def prepare(self, conn, asm):
        cache = VectorCache(cache_path(self.home))
        try:
            todo = missing(cache, self.identity, asm)
            if len(todo) > self.max_inline:
                raise NeedEmbedding(f"{len(todo)} chunks have no vector yet; run kb embed first (the commit builds on stored vectors; "
                                    f"it embeds at most {self.max_inline} by itself)")
            if todo:
                embed_missing(cache, self.embedder, self.identity, todo)
        finally:
            cache.close()

    def _matrix(self, cache, chunk_texts):
        """The rows for [(chunk_id, text)] in the given order, straight from the cache; a missing vector is an error, never a zero row."""
        shas = [ids.text_sha256(t) for _, t in chunk_texts]
        have = cache.get_many(self.identity.key(), set(shas))
        absent = [s for s in shas if s not in have]
        if absent:
            raise KbError(f"{len(absent)} vectors are missing from the cache at the moment of writing the artifact")
        return np.stack([have[s] for s in shas]) if shas else np.zeros((0, self.identity.dim), dtype=np.float32)

    def write(self, directory, asm):
        order = sorted((c.chunk_id, c.text) for c in asm.chunks)
        cache = VectorCache(cache_path(self.home))
        try:
            m = self._matrix(cache, order)
        finally:
            cache.close()
        path = os.path.join(directory, NAME)
        with open(path, "wb") as f:
            np.save(f, m, allow_pickle=False)
            f.flush()
            os.fsync(f.fileno())
        return {"rows": int(m.shape[0]), "dim": int(m.shape[1])}

    def check(self, directory, manifest, asm):
        """Problems found between the artifact and the commit's members (both directions: no row without a member, no member without a row)."""
        problems = []
        path = os.path.join(directory, NAME)
        try:
            m = np.load(path, allow_pickle=False, mmap_mode="r")
        except (OSError, ValueError) as e:
            return [f"{NAME} unreadable: {e}"]
        members = sorted(manifest["members"]["chunks"])
        if m.dtype != np.float32 or m.ndim != 2 or m.shape != (len(members), self.identity.dim):
            return problems + [f"{NAME} has shape {m.shape} {m.dtype}; expected ({len(members)}, {self.identity.dim}) float32 "
                               f"(one row per member chunk)"]
        if not np.isfinite(m).all():
            problems.append(f"{NAME} contains values that are not finite")
        if np.abs(np.linalg.norm(m, axis=1) - 1.0).max(initial=0.0) > NORM_TOLERANCE:
            problems.append(f"{NAME} contains rows that are not unit length")
        text_of = {c.chunk_id: c.text for c in asm.chunks}
        cache = VectorCache(cache_path(self.home))
        try:
            expected = self._matrix(cache, [(cid, text_of[cid]) for cid in members])
        except KbError as e:
            problems.append(str(e))
            expected = None
        finally:
            cache.close()
        if expected is not None and not np.array_equal(np.asarray(m), expected):
            bad = int((np.asarray(m) != expected).any(axis=1).sum())
            problems.append(f"{bad} rows of {NAME} are not the vector of the member chunk at that position")
        return problems

    def finish(self, conn, asm):
        cache = VectorCache(cache_path(self.home))
        try:
            return cache.keep_only(self.identity.key(), set(chunk_shas(asm)))
        finally:
            cache.close()
