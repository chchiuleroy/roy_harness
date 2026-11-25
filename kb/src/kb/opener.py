"""Open a search hit at the place it came from (Gate 1 H6, J1; Gate 2 section 4.13 `kb open`).

`resolve` turns a chunk id (or a source plus a page) into a file that can be opened and the position inside it, and says honestly whether
that file is still the one the index was built from: the file is hashed against the stored revision before it is offered, so a moved or changed
original is reported (`source_unavailable`, `source_changed`), never opened as if nothing happened (F2, T-S3). For a stored copy the blob has no
file name, so a named copy is written under `<home>/opened/<hash prefix>/` (derived, can be deleted at any time).

What is checked here is the TARGET that is handed to the viewer: the file and the page. The page is passed as a `#page=N` fragment of a file URL,
which Chrome, Edge and Firefox honour; another viewer may ignore it, and nothing in kb can see where a viewer finally lands.
"""
import json
import os
import pathlib
import shutil

from . import cas, commit
from .errors import KbError

OK, UNAVAILABLE, CHANGED = "ok", "source_unavailable", "source_changed"


def _current_revision(conn, source_id):
    row = conn.execute("SELECT r.revision_id, r.blob_sha256 FROM revision_observations o JOIN revisions r ON r.revision_id = o.revision_id "
                       "WHERE r.source_id = ? ORDER BY o.obs_id DESC LIMIT 1", (source_id,)).fetchone()
    if not row:
        raise KbError(f"source {source_id} has no revision")
    return row


def _name(conn, source_id):
    row = conn.execute("SELECT value FROM source_locations WHERE source_id=? AND kind='name' ORDER BY seq DESC LIMIT 1", (source_id,)).fetchone()
    return row[0] if row else "unnamed"


def _locator_of_chunk(conn, chunk_id):
    """(source id, locator, revision id, blob sha256): the position of the chunk AND the revision it was cut from, which is what the index holds."""
    row = conn.execute("SELECT unit_version_ids_json FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
    if not row:
        raise KbError(f"no chunk {chunk_id}")
    first = json.loads(row[0])[0]
    src = commit.source_of(conn, first)
    loc = conn.execute("SELECT cu.locator_json, r.revision_id, r.blob_sha256 FROM conversion_units cu JOIN conversions c ON c.conv_id = cu.conv_id "
                       "JOIN revisions r ON r.revision_id = c.revision_id WHERE cu.unit_version_id = ? ORDER BY cu.conv_id DESC LIMIT 1", (first,)).fetchone()
    return (src, json.loads(loc[0]), loc[1], loc[2]) if loc else (src, {}, None, None)


def _page_count(conn, source_id, revision_id=None):
    row = conn.execute("SELECT c.report_json FROM conversions c JOIN revisions r ON r.revision_id = c.revision_id WHERE r.source_id = ? AND c.converter = 'pdf' "
                       "AND c.status IN ('ok','partial') AND (? IS NULL OR r.revision_id = ?) ORDER BY c.rowid DESC LIMIT 1",
                       (source_id, revision_id, revision_id)).fetchone()
    return json.loads(row[0]).get("pages") if row else None


def _file_for(home, conn, store, sha, name):
    """(path, how, status): the original where it is when it is still the indexed file, else a named copy of the stored bytes."""
    brow = conn.execute("SELECT mode, path FROM blobs WHERE sha256 = ?", (sha,)).fetchone()
    if brow is None:
        return None, "none", UNAVAILABLE
    verdict = store.verify(conn, sha)
    if brow[0] == "ref":
        if verdict == cas.OK:
            return brow[1], "original", OK
        return (brow[1] if os.path.isfile(brow[1]) else None), "original", (CHANGED if verdict == cas.MISMATCH else UNAVAILABLE)
    if verdict != cas.OK:
        return None, "stored copy", (CHANGED if verdict == cas.MISMATCH else UNAVAILABLE)
    folder = os.path.join(home.path, "opened", sha[:16])
    path = os.path.join(folder, os.path.basename(name) or "file")
    if not os.path.isfile(path) or cas.hash_file(path)[0] != sha:
        os.makedirs(folder, exist_ok=True)
        shutil.copyfile(store.blob_path(sha), path)
    return path, "stored copy", OK


def resolve(home, conn, store, source_id, locator=None, page=None, revision=None):
    """The target for opening: {'source_id', 'name', 'path', 'how', 'status', 'page', 'bbox', 'url'}. `locator` is a hit's locator; `page`
    overrides it (open a source at a page without a hit). `revision` = (revision id, blob sha256) pins the file a hit was found in: a hit of the
    active commit opens THAT file (or says it is gone or changed), never a newer revision the commit has not seen. Without it the source's newest
    revision is opened. Raises KbError when the page does not exist in the file."""
    locator = dict(locator or {})
    if page is not None:
        locator = {"type": "pdf", "page": page}
    name = _name(conn, source_id)
    rev_id, sha = revision if revision and revision[1] else (None, _current_revision(conn, source_id)[1])
    path, how, status = _file_for(home, conn, store, sha, name)
    out = {"source_id": source_id, "name": name, "path": path, "how": how, "status": status, "page": locator.get("page"),
           "bbox": locator.get("bbox"), "locator": locator, "url": None}
    if locator.get("type") == "pdf" and out["page"] is not None:
        pages = _page_count(conn, source_id, rev_id)
        if not isinstance(out["page"], int) or out["page"] < 1 or (pages is not None and out["page"] > pages):
            raise KbError(f"{name} has {pages} pages; there is no page {out['page']}")
    if path and status == OK:
        out["url"] = pathlib.Path(os.path.abspath(path)).as_uri() + (f"#page={out['page']}" if out["page"] else "")
    return out


def chunk_source(conn, chunk_id):
    """The source a chunk of the ACTIVE commit was cut from. A chunk id that is not in the active commit (an old version, a hidden source that has
    been committed away, an invented id) opens nothing: knowing an id is not an access right."""
    active = commit.active_commit_id(conn)
    if not active or conn.execute("SELECT 1 FROM commit_chunks WHERE commit_id = ? AND chunk_id = ?", (active, chunk_id)).fetchone() is None:
        raise KbError(f"chunk {chunk_id} is not part of the active commit (it may come from an older version or from a source that is no longer indexed)")
    source_id = _locator_of_chunk(conn, chunk_id)[0]
    if not source_id:
        raise KbError(f"chunk {chunk_id} has no source")
    if commit.tombstone_level(conn, source_id) == "b":                  # hidden from general access takes effect now, not at the next commit
        raise KbError(f"{chunk_id!r} matches 0 chunks")
    return source_id


def resolve_chunk(home, conn, store, chunk_id):
    source_id = chunk_source(conn, chunk_id)
    _, locator, rev_id, sha = _locator_of_chunk(conn, chunk_id)
    return resolve(home, conn, store, source_id, locator, revision=(rev_id, sha))
