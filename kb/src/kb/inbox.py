"""Intake (Gate 2 section 4.5): one action puts a file, a folder, pasted text or a pasted URL into the knowledge base.
Success means 'persisted and traceable', NOT 'searchable': the new revision waits for conversion and the next commit (a candidate).
Nothing is ever dropped silently: anything refused or deferred is written to intake_skips with the reason (Gate 1 P4, H5).

Identity rules: the same location with the same bytes is 'unchanged'; the same location with other bytes is an 'updated' revision of the
same source; identical bytes appearing at a new path while the old path is gone is a 'renamed' source (same source id, one more location);
identical bytes at a new path while the old one still exists is a 'duplicate_blob' (a new source sharing the stored blob).
The caller holds the writer lock.
"""
import hashlib
import os
import stat
import time
from dataclasses import dataclass
from typing import Optional

from . import fetch as fetchmod
from . import ids, pathsafe
from .errors import BlobError, KbError


@dataclass
class Result:
    outcome: str                      # new | unchanged | updated | renamed | duplicate_blob | skipped | failed
    source_id: Optional[str] = None
    revision_id: Optional[str] = None
    path: Optional[str] = None
    reason: Optional[str] = None


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm(path):
    return os.path.abspath(path).replace(os.sep, "/")


def _identity(st):
    return (st.st_size, st.st_mtime_ns, getattr(st, "st_ino", 0), getattr(st, "st_dev", 0))


class Intake:
    def __init__(self, conn, store, collection_id, settle_s=1.0, clock=time.time, fetcher=None, resolver=None):
        self.conn, self.store, self.collection_id = conn, store, collection_id
        self.settle_s, self.clock = settle_s, clock
        self.fetcher = fetcher or fetchmod.fetch
        self.resolver = resolver

    # ------------------------------------------------------------------ entry points
    def add_file(self, path, mode="copy", boundary=None):
        """boundary: when the file came from a folder scan, the folder it must stay inside (re-checked after the read, see the note below)."""
        path = os.path.abspath(path)
        lp = pathsafe.long_path(path)
        problem = pathsafe.entry_problem(path, os.path.basename(path), False)
        if problem:
            return self._skip(path, problem)
        if boundary and not pathsafe.is_inside(boundary, path):
            return self._skip(path, "outside_the_folder")
        try:
            st1 = os.stat(lp)
        except FileNotFoundError:
            return self._skip(path, "missing")
        if not stat.S_ISREG(st1.st_mode):
            return self._skip(path, "not_a_regular_file")
        if self.clock() - st1.st_mtime < self.settle_s:
            return self._skip(path, "unstable_retry_later")        # still being written: a half-written file is never taken
        try:
            with open(lp, "rb") as f:
                f.read(1)
        except OSError:
            return self._skip(path, "temporarily_unreadable")       # locked by another program: try again later
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                ref = self.store.put_path(self.conn, lp, mode)
            except (PermissionError, OSError) as e:
                self.conn.execute("ROLLBACK")
                return self._skip(path, "temporarily_unreadable" if isinstance(e, PermissionError) else f"unreadable:{type(e).__name__}")
            # Narrowed, not closed: the path is opened by name, so something with write access to the folder could still swap it for a link
            # between a check and the read. After the read the file identity (size, mtime, inode and device where they exist), the link
            # flags and the boundary are checked again and the record is dropped if anything moved. Closing the gap needs a handle-based open.
            st2 = os.stat(lp)
            after = pathsafe.entry_problem(path, os.path.basename(path), False) or (
                "outside_the_folder" if boundary and not pathsafe.is_inside(boundary, path) else None)
            if after or _identity(st2) != _identity(st1):
                self.conn.execute("ROLLBACK")
                return self._skip(path, after or "changed_while_reading")
            res = self._register(ref, "path", _norm(path), os.path.basename(path), "file")
            self.conn.execute("DELETE FROM intake_skips WHERE collection_id=? AND path=?", (self.collection_id, _norm(path)))
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        res.path = _norm(path)
        return res

    def add_folder(self, root, mode="copy"):
        results = []
        for entry in pathsafe.scan_folder(root):
            results.append(self._skip(entry.path, entry.skip) if entry.skip else self.add_file(entry.path, mode, boundary=root))
        return results

    def add_text(self, text, name=None):
        data = text.encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        name = name or f"pasted-{sha[:8]}.txt"
        return self._add_bytes(data, "paste", f"paste:{name}", name, "paste")

    def add_url(self, url, allow_private=False):
        try:
            kwargs = {"allow_private": allow_private}
            if self.resolver:
                kwargs["resolver"] = self.resolver
            data, ctype, final = self.fetcher(url, **kwargs)
        except fetchmod.FetchRefused as e:
            return self._skip(url, f"fetch_refused:{e}")
        return self._add_bytes(data, "url", url, fetchmod.filename_hint(final or url, ctype), "url")

    # ------------------------------------------------------------------ internals
    def _add_bytes(self, data, loc_kind, loc_value, name, source_kind):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            ref = self.store.put_bytes(self.conn, data)
            res = self._register(ref, loc_kind, loc_value, name, source_kind)
            self.conn.execute("DELETE FROM intake_skips WHERE collection_id=? AND path=?", (self.collection_id, loc_value))
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        res.path = loc_value
        return res

    def _skip(self, path, reason):
        p = _norm(path) if os.path.isabs(path) else path
        self.conn.execute("INSERT OR REPLACE INTO intake_skips(collection_id, path, reason, observed_at) VALUES (?,?,?,?)",
                          (self.collection_id, p, reason, _now()))
        return Result("skipped", path=p, reason=reason)

    def current_revision(self, source_id):
        return self.conn.execute(
            "SELECT r.revision_id, r.blob_sha256 FROM revision_observations o JOIN revisions r ON r.revision_id = o.revision_id "
            "WHERE r.source_id = ? ORDER BY o.obs_id DESC LIMIT 1", (source_id,)).fetchone()

    def _find_by_location(self, kind, value):
        col = "lower(l.value) = lower(?)" if os.name == "nt" and kind == "path" else "l.value = ?"
        row = self.conn.execute(
            f"SELECT s.source_id FROM sources s JOIN source_locations l ON l.source_id = s.source_id "
            f"WHERE s.collection_id = ? AND l.kind = ? AND {col} ORDER BY l.seq DESC LIMIT 1", (self.collection_id, kind, value)).fetchone()
        return row[0] if row else None

    def _sources_with_blob(self, sha):
        out = []
        for (sid,) in self.conn.execute("SELECT DISTINCT s.source_id FROM sources s JOIN revisions r ON r.source_id = s.source_id "
                                        "WHERE s.collection_id = ? AND r.blob_sha256 = ?", (self.collection_id, sha)).fetchall():
            cur = self.current_revision(sid)
            if cur and cur[1] == sha:
                out.append(sid)
        return out

    def _latest_location(self, source_id, kind):
        row = self.conn.execute("SELECT value FROM source_locations WHERE source_id=? AND kind=? ORDER BY seq DESC LIMIT 1",
                                (source_id, kind)).fetchone()
        return row[0] if row else None

    def _add_location(self, source_id, kind, value):
        seq = self.conn.execute("SELECT coalesce(max(seq), 0) + 1 FROM source_locations WHERE source_id=?", (source_id,)).fetchone()[0]
        self.conn.execute("INSERT INTO source_locations(source_id, seq, kind, value, observed_at) VALUES (?,?,?,?,?)",
                          (source_id, seq, kind, value, _now()))

    def _register(self, ref, loc_kind, loc_value, name, source_kind):
        c, now = self.conn, _now()
        existing = self._find_by_location(loc_kind, loc_value)
        if existing:
            cur = self.current_revision(existing)
            if cur and cur[1] == ref.sha256:
                return Result("unchanged", existing, cur[0])
            rev = ids.revision_id(existing, ref.sha256)
            c.execute("INSERT OR IGNORE INTO revisions(revision_id, source_id, blob_sha256) VALUES (?,?,?)", (rev, existing, ref.sha256))
            c.execute("INSERT INTO revision_observations(revision_id, seen_at, parent_revision_id) VALUES (?,?,?)",
                      (rev, now, cur[0] if cur else None))
            return Result("updated", existing, rev)
        same = self._sources_with_blob(ref.sha256)
        ambiguous = False
        if loc_kind == "path":
            gone = [sid for sid in same if (lambda old: old and not os.path.exists(pathsafe.long_path(old)))(self._latest_location(sid, "path"))]
            if len(gone) == 1:
                sid = gone[0]
                self._add_location(sid, "path", loc_value)
                self._add_location(sid, "name", name)
                return Result("renamed", sid, self.current_revision(sid)[0])
            ambiguous = len(gone) > 1               # several sources with these bytes lost their files: do not guess which one moved
        sid = ids.new_id("src")
        c.execute("INSERT INTO sources(source_id, collection_id, kind, created_at) VALUES (?,?,?,?)", (sid, self.collection_id, source_kind, now))
        self._add_location(sid, loc_kind, loc_value)
        self._add_location(sid, "name", name)
        rev = ids.revision_id(sid, ref.sha256)
        c.execute("INSERT INTO revisions(revision_id, source_id, blob_sha256) VALUES (?,?,?)", (rev, sid, ref.sha256))
        c.execute("INSERT INTO revision_observations(revision_id, seen_at, parent_revision_id) VALUES (?,?,NULL)", (rev, now))
        if ambiguous:
            return Result("new", sid, rev, reason="ambiguous_rename")
        return Result("duplicate_blob" if same else "new", sid, rev)
