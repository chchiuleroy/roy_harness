"""Semantic ranking over the vector artifact of the active commit (Gate 2 section 4.11, stage S3; tier T1).

`Dense.rank` embeds the query with the backend, compares it with every row of `vec.npy` (a dot product; all vectors have unit length) and
returns the best chunks. Before it does anything it compares the identity recorded in the commit's manifest with the identity of the backend
that is configured now: a vector index built by another model, another dimension or another instruction is REFUSED, not used with a warning
(F10, T-I2: mixed vectors give a quiet, hard to notice drop in quality). The caller then falls back to lexical search or tells the person.

The matrix is opened memory-mapped once per commit, so a second query does not read the file again.
"""
import os

import numpy as np

from . import vecindex
from .errors import VectorUnavailable


class Dense:
    def __init__(self, embedder):
        self.embedder = embedder
        self._loaded = None                  # (commit_id, matrix, member chunk ids in row order)

    def why_unusable(self, manifest):
        """None when the commit's vectors can be searched with the backend in use, otherwise the reason in plain words."""
        ext = (manifest.get("extensions") or {}).get(vecindex.NAME)
        if ext is None:
            return "this commit has no vector index; run kb embed and kb commit"
        mine = vecindex.signature_of(self.embedder.identity)
        if ext["signature"] != mine:
            was, now = ext["signature"], mine
            differs = sorted(k for k in set(was) | set(now) if was.get(k) != now.get(k))
            return (f"the vector index was built under another identity ({', '.join(differs)} differ); it is not used. "
                    f"Run kb embed and kb commit to rebuild it")
        return None

    def _matrix(self, cid, manifest, directory):
        if self._loaded and self._loaded[0] == cid:
            return self._loaded[1], self._loaded[2]
        members = sorted(manifest["members"]["chunks"])
        m = np.load(os.path.join(directory, vecindex.NAME), mmap_mode="r", allow_pickle=False)
        if m.ndim != 2 or m.shape[0] != len(members):
            raise VectorUnavailable(f"{vecindex.NAME} does not match the commit's members ({m.shape[0] if m.ndim == 2 else '?'} rows for {len(members)} chunks)")
        self._loaded = (cid, m, members)
        return m, members

    def rank(self, query, cid, manifest, directory, limit):
        reason = self.why_unusable(manifest)
        if reason:
            raise VectorUnavailable(reason)
        m, members = self._matrix(cid, manifest, directory)
        q = self.embedder.embed_query(query)
        sims = np.asarray(m @ q)
        n = min(limit, len(members))
        top = np.argsort(-sims, kind="stable")[:n]                        # best first; ties keep the row order (a partial sort gives no such promise)
        return [(members[i], manifest["chunks"][members[i]]["source_id"], float(sims[i])) for i in top]
