"""Search over the active commit (Gate 1 J2, J3; Gate 2 section 4.11).

Committed results come from the active commit's index and carry what a person needs to trust them: the source (title, name, where it came
from), where in it the text sits, the words that matched ('why'), a snippet, and honest status: whether the original is still available and
unchanged, and whether a newer revision exists that the index has not seen. Chunks with identical text from different sources are collapsed into
one hit that lists the other places ('also_in'). 'Candidates' are sources the active commit does not cover yet (new or newer than the index):
they are searched in memory, ranked by a simple word-overlap score, and always labelled as unprocessed candidates, never as evidence.
"""
import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional

from . import cas, chunker, colls, commit, conv_registry, ftsindex, pipeline, profile
from . import tokenize as tk
from .errors import PolicyViolation, VectorUnavailable

RRF_K = 60
MODES = ("lexical", "dense", "hybrid", "auto")


def rrf(lists, k=RRF_K):
    """Reciprocal rank fusion of several ranked lists of (chunk_id, source_id, score): the score of an item is the sum of 1/(k + rank)."""
    fused, source = {}, {}
    for lst in lists:
        for rank, (chunk_id, source_id, _) in enumerate(lst, 1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
            source[chunk_id] = source_id
    return [(c, source[c], s) for c, s in sorted(fused.items(), key=lambda x: (-x[1], x[0]))]


@dataclass
class Hit:
    state: str                         # committed | candidate
    source_id: str
    chunk_id: Optional[str]
    score: float
    title: str
    name: str
    location: str
    snippet: str
    locator: dict = field(default_factory=dict)
    why: List[str] = field(default_factory=list)
    status: str = "ok"                 # ok | source_unavailable | source_changed
    outdated: bool = False             # a newer revision of this source exists than the one the index was built from
    also_in: List[str] = field(default_factory=list)


def _temporary_uv(i):
    """Placeholder unit-version ids for in-memory candidate chunking (they never reach the database)."""
    return "uv_" + base64.b32encode(i.to_bytes(15, "big")).decode("ascii").lower().rstrip("=")


def _snippet(text, matched, width=160):
    low = text.lower()
    pos = min((low.find(m) for m in matched if low.find(m) >= 0), default=0)
    start = max(0, pos - width // 4)
    piece = text[start:start + width].replace("\n", " ")
    return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")


class Searcher:
    def __init__(self, home, conn, dense=None):
        """`dense` is the optional semantic ranker (tier T1, see dense.py); without it every search is lexical."""
        self.home, self.conn, self.dense = home, conn, dense
        self.store = cas.BlobStore(home.sub("blobs"))
        self.notices = []                       # why an automatic search fell back to lexical (shown by the command line, never hidden)
        self._via = {}

    # ------------------------------------------------------------------ provenance helpers
    def _source_info(self, source_id):
        title = self.conn.execute(
            "SELECT json_extract(s.value_json, '$') FROM statements s JOIN commit_statements cs ON cs.statement_id = s.statement_id "
            "WHERE cs.commit_id = (SELECT commit_id FROM active_commit WHERE id = 1) AND s.subject_id = ? AND s.key = 'title' AND cs.role = 'display'",
            (source_id,)).fetchone()
        name = self.conn.execute("SELECT value FROM source_locations WHERE source_id=? AND kind='name' ORDER BY seq DESC LIMIT 1", (source_id,)).fetchone()
        loc = self.conn.execute("SELECT value FROM source_locations WHERE source_id=? AND kind != 'name' ORDER BY seq DESC LIMIT 1", (source_id,)).fetchone()
        return (title[0] if title else None) or (name[0] if name else source_id), name[0] if name else "", loc[0] if loc else ""

    def _current_revision(self, source_id):
        row = self.conn.execute("SELECT r.revision_id, r.blob_sha256 FROM revision_observations o JOIN revisions r ON r.revision_id = o.revision_id "
                                "WHERE r.source_id = ? ORDER BY o.obs_id DESC LIMIT 1", (source_id,)).fetchone()
        return row if row else (None, None)

    def _status(self, source_id, revision_id_in_index):
        """Check the evidence behind a hit: is the original there, and is it still the bytes the index was built from?"""
        sha_row = self.conn.execute("SELECT blob_sha256 FROM revisions WHERE revision_id=?", (revision_id_in_index,)).fetchone()
        if not sha_row:
            return "source_unavailable", False
        verdict = self.store.verify(self.conn, sha_row[0], cached=True)
        status = "ok" if verdict == cas.OK else ("source_unavailable" if verdict == cas.MISSING else "source_changed")
        current_rev, _ = self._current_revision(source_id)
        return status, current_rev is not None and current_rev != revision_id_in_index

    def _locator(self, chunk_id, source_id):
        row = self.conn.execute("SELECT unit_version_ids_json FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        if not row:
            return {}
        first = json.loads(row[0])[0]
        loc = self.conn.execute(
            "SELECT cu.locator_json FROM conversion_units cu JOIN conversions c ON c.conv_id = cu.conv_id JOIN revisions r ON r.revision_id = c.revision_id "
            "WHERE cu.unit_version_id = ? AND r.source_id = ? ORDER BY c.rowid DESC LIMIT 1", (first, source_id)).fetchone()
        return json.loads(loc[0]) if loc else {}

    # ------------------------------------------------------------------ committed search
    def _active(self):
        """The active commit's manifest and index path. The manifest is large (it lists every chunk), so it is parsed once per commit and
        reused; each call still re-checks that the index file is there and has the recorded size."""
        cached = getattr(self, "_cache", None)
        cid = commit.active_commit_id(self.conn)
        if cached and cid == cached[0]:
            try:
                if os.path.getsize(cached[2]) == cached[1]["artifacts"][commit.FTS]["size"]:
                    return cached
            except OSError:
                pass
        cid, manifest, fts = commit.active_artifacts(self.home, self.conn)
        self._cache = (cid, manifest, fts)
        return self._cache

    def _dense_list(self, query, limit, cid, manifest, fts):
        return self.dense.rank(query, cid, manifest, os.path.dirname(fts), limit)

    def _ranked(self, query, limit, mode="lexical"):
        """Chunk-level ranking, identical chunk texts collapsed. Cheap: no provenance is looked up here. Returns [(group, manifest)] with
        group = [(chunk_id, source_id, score), ...] best first. `mode`: lexical | dense | hybrid (reciprocal rank fusion of both, k=60) | auto
        (hybrid when a usable vector index exists, otherwise lexical with a notice). Both lists come out of the SAME commit's members, so a chunk
        that is not a member is in neither (T-S1)."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        cid, manifest, fts = self._active()
        dense_list = None
        # Embedding the query is a wait on the model server and the lexical search is a wait on SQLite; neither holds the interpreter lock, so when both
        # are needed they run side by side. The results are the ones the sequential order gives (nothing is shared between the two calls).
        pool = ThreadPoolExecutor(max_workers=1) if mode in ("hybrid", "auto") and self.dense is not None else None
        try:
            lexical_job = pool.submit(ftsindex.search, fts, query, limit) if pool else None
            if mode in ("dense", "hybrid", "auto"):
                try:
                    if self.dense is None:
                        raise VectorUnavailable("no embedding backend is attached to this search")
                    dense_list = self._dense_list(query, limit, cid, manifest, fts)
                except VectorUnavailable as e:
                    if mode != "auto":
                        raise
                    self.notices.append(f"semantic search not used ({e}); lexical results only")
                    mode = "lexical"
            if mode == "dense":
                lexical_list = []
            else:
                lexical_list = lexical_job.result() if lexical_job else ftsindex.search(fts, query, limit=limit)
        finally:
            if pool:
                pool.shutdown(wait=True)             # also when the dense side raised: nothing is left running behind the caller. An interrupt therefore waits
                                                     # for the one lexical query in flight (tens of ms; measured median about 50 ms); accepted, see docs/S4_REVIEW.md
        self._via = {}
        for rank, (c, _, _) in enumerate(lexical_list, 1):
            self._via.setdefault(c, {})["lexical"] = rank
        for rank, (c, _, _) in enumerate(dense_list or [], 1):
            self._via.setdefault(c, {})["dense"] = rank
        ranked = rrf([lexical_list, dense_list]) if dense_list is not None and lexical_list else (dense_list if dense_list is not None else lexical_list)
        groups, order = {}, []
        for chunk_id, source_id, score in ranked:
            text_sha = manifest["chunks"][chunk_id]["text_sha256"]
            if text_sha not in groups:
                groups[text_sha] = []
                order.append(text_sha)
            groups[text_sha].append((chunk_id, source_id, score))
        return [groups[t] for t in order], manifest

    def _hit(self, group, manifest, qtokens):
        """Provenance and honesty checks, done only for the hits that are actually returned (the status check hashes the original file)."""
        (chunk_id, source_id, score), *others = group
        text = self.conn.execute("SELECT text FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
        toks = set(tk.tokens(text))
        matched = [t for t in qtokens if t in toks]
        via = self._via.get(chunk_id, {})
        title, name, location = self._source_info(source_id)
        status, outdated = self._status(source_id, manifest["chunks"][chunk_id]["revision_id"])
        return Hit("committed", source_id, chunk_id, score, title, name, location, _snippet(text, matched), self._locator(chunk_id, source_id),
                   matched[:8] + (["semantic match"] if "dense" in via else []), status, outdated, [o[1] for o in others])

    def chunks(self, query, k=10, mode="lexical"):
        ranked, manifest = self._ranked(query, max(k * 4, 20), mode)
        qtokens = sorted(set(tk.tokens(query)))
        return [self._hit(g, manifest, qtokens) for g in ranked[:k]]

    def sources(self, query, k=10, mode="lexical"):
        """Page-level view: the best chunk of each source, best source first (the unit the old wiki evaluation scored)."""
        ranked, manifest = self._ranked(query, max(k * 8, 60), mode)
        qtokens = sorted(set(tk.tokens(query)))
        seen, picked = set(), []
        for g in ranked:
            if g[0][1] not in seen:
                seen.add(g[0][1])
                picked.append(g)
            if len(picked) == k:
                break
        return [self._hit(g, manifest, qtokens) for g in picked]

    # ------------------------------------------------------------------ candidates (not committed yet)
    def candidates(self, query, k=10):
        try:
            _, manifest, _ = commit.active_artifacts(self.home, self.conn)
            covered = {}
            for info in manifest["chunks"].values():
                covered[info["source_id"]] = info["revision_id"]
        except Exception:
            covered = {}
        qtokens = set(tk.tokens(query))
        if not qtokens:
            return []
        hits = []
        for (source_id,) in self.conn.execute("SELECT source_id FROM sources ORDER BY source_id"):
            rev, sha = self._current_revision(source_id)
            if rev is None or covered.get(source_id) == rev:
                continue
            collection_id = self.conn.execute("SELECT collection_id FROM sources WHERE source_id=?", (source_id,)).fetchone()[0]
            try:
                profile.require_index(colls.get_profile(self.conn, collection_id))
            except PolicyViolation:
                continue                                    # a collection that must not be indexed is not searched as a candidate either
            data = pipeline._read_blob(self.conn, self.store, sha)
            name = self._source_info(source_id)[1]
            converter = conv_registry.pick(name, (data or b"")[:512]) if data is not None else None
            if converter is None:
                continue
            res = converter.convert(data, {"filename": name})
            units = [(_temporary_uv(i), u.text, tuple(u.heading_path)) for i, u in enumerate(res.units)]
            best = None
            for ch in chunker.chunk_units(units):
                toks = set(tk.tokens(ch.text))
                overlap = len(qtokens & toks)
                if overlap and (best is None or overlap > best[0]):
                    best = (overlap, ch.text, sorted(qtokens & toks))
            if best:
                title = (res.meta.get("title") or name)
                _, _, location = self._source_info(source_id)
                hits.append(Hit("candidate", source_id, None, best[0] / len(qtokens), title, name, location, _snippet(best[1], best[2]),
                                {}, best[2][:8], "ok", False, []))
        hits.sort(key=lambda h: -h.score)
        return hits[:k]
