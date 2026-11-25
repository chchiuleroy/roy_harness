"""Conversion pipeline (Gate 2 sections 4.6-4.7): every revision without a conversion is converted; the result becomes units, unit
versions and provenance-bearing statements. Failures are rows too: nothing is silently missing (Gate 1 P4, H5, W3).

A unit keeps its identity across revisions of a source when its structural key (kind, heading path, n-th of that kind under that
heading) matches the earlier conversion; a text edit then produces a new unit VERSION of the same unit, while units whose text did not
change keep their unit version (and so their chunk ids) even when the text before them moved. Character offsets are stored per conversion
in conversion_units.locator_json, not in the unit version's identity. Limit: a paragraph inserted in the middle shifts the index of the
paragraphs after it under the same heading, so those are re-versioned; key matching by content similarity belongs to stage S2.
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass

from . import cas, conv_base, conv_registry, ids
from .canonical import canonical_bytes

SOURCE_UNAVAILABLE = "source_unavailable"


@dataclass
class Processed:
    revision_id: str
    status: str                       # ok | partial | failed
    reason: str = ""
    units: int = 0


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def unit_key(unit, counters):
    """Structural identity of a unit inside its document: kind + heading path + running index of that kind under that heading. The heading path enters as a keyed
    digest: the key is stored in the database and must not hold the headings of something that is hidden later (ninth review)."""
    base = (unit.kind, tuple(unit.heading_path))
    counters[base] = counters.get(base, -1) + 1
    return json.dumps([unit.kind, ids.keyed_sha256(canonical_bytes(list(unit.heading_path))), counters[base]], ensure_ascii=False)


def unit_output_sha256(unit):
    return ids.keyed_sha256(canonical_bytes({"kind": unit.kind, "text": unit.text, "heading_path": list(unit.heading_path)}))


def _read_blob(conn, store, sha):
    row = conn.execute("SELECT mode, path, size FROM blobs WHERE sha256=?", (sha,)).fetchone()
    path = row[1] if row[0] == "ref" else store.blob_path(sha)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return data if hashlib.sha256(data).hexdigest() == sha else None      # a changed or damaged original is not converted


def _latest_name(conn, source_id):                 # also used by upgrade.py
    row = conn.execute("SELECT value FROM source_locations WHERE source_id=? AND kind='name' ORDER BY seq DESC LIMIT 1", (source_id,)).fetchone()
    return row[0] if row else "unnamed"


def pending_revisions(conn):
    return [r[0] for r in conn.execute(
        "SELECT r.revision_id FROM revisions r WHERE NOT EXISTS (SELECT 1 FROM conversions c WHERE c.revision_id = r.revision_id) "
        "ORDER BY r.rowid")]


def _previous_unit_ids(conn, source_id, exclude_conv):
    """key -> unit_id from the most recent usable conversion of this source other than the one being made (of an earlier revision, or of the
    same revision when a newer converter version converts it again: unit identity must survive both)."""
    row = conn.execute(
        "SELECT c.conv_id FROM conversions c JOIN revisions r ON r.revision_id = c.revision_id WHERE r.source_id = ? AND c.conv_id != ? "
        "AND c.status IN ('ok','partial') ORDER BY c.rowid DESC LIMIT 1", (source_id, exclude_conv)).fetchone()
    if not row:
        return {}
    out = {}
    for key_json, unit_id in conn.execute(
            "SELECT json_extract(uv.locator_json, '$.key'), uv.unit_id FROM conversion_units cu "
            "JOIN unit_versions uv ON uv.unit_version_id = cu.unit_version_id WHERE cu.conv_id = ?", (row[0],)):
        out[key_json] = unit_id
    return out


def _hidden_ids_by_text(conn, source_id):
    """fingerprint of the text of any version of a hidden (level a or b) unit of this source -> [unit_id, ...]. A new revision may move a paragraph so that
    its structural key changes; the key then gives it a new unit id that no tombstone names, and hidden text would be visible again. The same text
    takes the hidden id back (seventh review, 2026-11-24). Text that was edited or cut differently is not recognised: identity by text is exact."""
    out = {}
    for fp, unit_id in conn.execute(
            "SELECT DISTINCT uv.fingerprint, uv.unit_id FROM tombstones t JOIN unit_versions uv ON uv.unit_id = t.target_id "
            "JOIN conversions c ON c.conv_id = uv.conv_id JOIN revisions r ON r.revision_id = c.revision_id "
            "WHERE t.level IN ('a', 'b') AND r.source_id = ? ORDER BY uv.unit_id", (source_id,)):
        out.setdefault(fp, []).append(unit_id)
    return out


def _assign_unit_ids(units, keys, previous, hidden_by_text):
    """One unit id (or None for a new one) per unit, never the same id twice in a conversion. A unit whose text equals hidden text takes that hidden id
    first; the rest reuse the id of their structural key."""
    chosen, used = [None] * len(units), set()
    spare = {fp: list(v) for fp, v in hidden_by_text.items()}
    for i, unit in enumerate(units):
        candidates = spare.get(ids.fingerprint(unit.text))
        while candidates and candidates[0] in used:          # the same hidden unit also under another version of its text (eighth review)
            candidates.pop(0)
        if candidates:
            chosen[i] = candidates.pop(0)
            used.add(chosen[i])
    for i, key in enumerate(keys):
        if chosen[i] is None and previous.get(key) not in used:
            chosen[i] = previous.get(key)
            if chosen[i] is not None:
                used.add(chosen[i])
    return chosen


def process_revision(conn, store, revision_id, converter=None):
    """Convert one revision. `converter` forces a specific converter (an upgrade re-converting with the new version); by default the registry picks."""
    source_id, sha = conn.execute("SELECT source_id, blob_sha256 FROM revisions WHERE revision_id=?", (revision_id,)).fetchone()
    name = _latest_name(conn, source_id)
    conv_id = ids.new_id("conv")
    now = _now()

    def record(converter_name, version, params, status, report, output_sha=None):
        ident = {"converter": converter_name, "version": version, "params": params}
        conn.execute("INSERT INTO conversions(conv_id, revision_id, converter, version, params_hash, exec_identity_json, status, output_sha256, "
                     "report_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (conv_id, revision_id, converter_name, version, hashlib.sha256(canonical_bytes(params)).hexdigest(),
                      json.dumps(ident, sort_keys=True), status, output_sha, json.dumps(report, ensure_ascii=False, sort_keys=True), now))

    data = _read_blob(conn, store, sha)
    if data is None:
        record("none", "0", {}, "failed", {"reason": SOURCE_UNAVAILABLE, "name": name})
        return Processed(revision_id, "failed", SOURCE_UNAVAILABLE)
    converter = converter or conv_registry.pick(name, data[:512])
    if converter is None:
        record("none", "0", {}, "failed", {"reason": "unsupported_type", "name": name})
        return Processed(revision_id, "failed", "unsupported_type")
    result = converter.convert(data, {"filename": name})
    ident_params = converter.params
    out_sha = ids.keyed_sha256(canonical_bytes([[u.kind, u.text, list(u.heading_path)] for u in result.units]))
    report = dict(result.report, status=result.status, warnings=result.warnings, converter=converter.name, version=converter.version)
    record(converter.name, converter.version, ident_params, result.status, report, out_sha)
    if result.status == "failed":
        return Processed(revision_id, "failed", (result.warnings or ["failed"])[0])
    previous = _previous_unit_ids(conn, source_id, conv_id)
    counters = {}
    keys = [unit_key(unit, counters) for unit in result.units]
    chosen = _assign_unit_ids(result.units, keys, previous, _hidden_ids_by_text(conn, source_id))
    for ordinal, unit in enumerate(result.units):
        key = keys[ordinal]
        unit_id = chosen[ordinal]
        if unit_id is None:
            unit_id = ids.new_id("unit")
            conn.execute("INSERT INTO units(unit_id, created_at) VALUES (?,?)", (unit_id, now))
        stable = {"key": key}                                   # identity: structure only, never offsets
        fp = ids.fingerprint(unit.text)
        uv = ids.unit_version_id(unit_id, unit_output_sha256(unit), stable, fp)
        conn.execute("INSERT OR IGNORE INTO unit_versions(unit_version_id, unit_id, conv_id, kind, locator_json, fingerprint) VALUES (?,?,?,?,?,?)",
                     (uv, unit_id, conv_id, unit.kind, json.dumps(stable, ensure_ascii=False, sort_keys=True), fp))
        conn.execute("INSERT INTO conversion_units(conv_id, ordinal, unit_version_id, locator_json) VALUES (?,?,?,?)",
                     (conv_id, ordinal, uv, json.dumps(dict(unit.locator, key=key, heading=list(unit.heading_path)),
                                                       ensure_ascii=False, sort_keys=True)))
        stmt(conn, uv, "text", unit.text, "native", {"conv_id": conv_id})
    title = result.meta.get("title")
    if title:
        stmt(conn, source_id, "title", title, "native", {"conv_id": conv_id})
    else:
        stmt(conn, source_id, "title", os.path.splitext(name)[0], "derived", {"conv_id": conv_id, "from": "file name"})
    if converter.name == "markdown":
        for target in result.meta.get("links", []):           # wikilinks as written; relations.derive resolves them to sources
            stmt(conn, source_id, "links_to", target, "native", {"conv_id": conv_id})
    fm = result.meta.get("frontmatter") or {}
    tags = fm.get("tags")
    for t in (tags if isinstance(tags, list) else []):
        stmt(conn, source_id, "tag", t, "native", {"conv_id": conv_id})
    return Processed(revision_id, result.status, "; ".join(result.warnings), len(result.units))


def stmt(conn, subject_id, key, value, origin, evidence):
    sid = ids.statement_id(subject_id, key, value, origin, evidence)
    conn.execute("INSERT OR IGNORE INTO statements(statement_id, subject_id, key, value_json, origin, evidence_json, state) "
                 "VALUES (?,?,?,?,?,?, 'candidate')", (sid, subject_id, key, json.dumps(value, ensure_ascii=False), origin,
                                                       json.dumps(evidence, sort_keys=True)))
    return sid


def process_pending(conn, store):
    """Convert every revision that has no conversion yet. Each revision is its own transaction, so a crash leaves whole conversions only."""
    out = []
    for rev in pending_revisions(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            out.append(process_revision(conn, store, rev))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return out
