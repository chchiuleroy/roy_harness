"""The commit protocol (Gate 2 section 4.10). A commit publishes one consistent version of the knowledge: members (unit versions, statements,
chunks), a lexical index file and a manifest that commits to all of it.

    1 begin       row in `commits` (staging) and an empty staging folder
    2 assemble    members and chunks written to the database; index and manifest written to the staging folder (fsync, close)
    3 verify      artifacts re-read and re-hashed; ghost check (index content equals members, both directions, ALL chunks); state 'verified'
    4 journal     state 'publishing' (the intent to publish is on record before anything is renamed)
    5 publish     staging folder renamed to segments/<commit_id>, directory synced, artifacts read back and re-hashed
    6 activate    ONE database transaction: previous active -> retired, this one -> active, active_commit, statements -> committed
    7 recover     at startup: a 'publishing' commit is completed (artifacts found) or abandoned (not); abandoned work is cleaned

Any failure before step 6 leaves the previous active commit untouched and the new one 'abandoned'. After a crash, recover() guarantees that
the active commit is complete and readable: either the new one or the previous one. Damage found LATER (a deleted file, bit rot) is not
repaired silently: serving refuses with ArtifactsMissing and `rollback` is an explicit step.
"""
import calendar
import hashlib
import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List

from . import audit, chunker, colls, display, ftsindex, ids, profile
from . import tokenize as tk
from .canonical import canonical_bytes
from .durable import fsync_dir
from .errors import ArtifactsMissing, CommitFailed, KbError, PolicyViolation
from .faults import fault_point

MANIFEST = "manifest.json"
FTS = "fts.db"
FORMAT = 1
RENAME_TRIES = 6


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _write_file(path, data):
    """Separate function so tests can make it fail the way a full disk does."""
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _rename_dir(src, dst):
    """Separate function so tests can simulate a program holding a handle (PermissionError) or a failing rename."""
    os.rename(src, dst)


# ------------------------------------------------------------------------------------------------ assembly
@dataclass
class Assembly:
    unit_versions: List[str] = field(default_factory=list)
    statements: Dict[str, str] = field(default_factory=dict)            # statement_id -> role
    chunks: List[chunker.Chunk] = field(default_factory=list)
    chunk_source: Dict[str, str] = field(default_factory=dict)          # chunk_id -> source_id
    chunk_revision: Dict[str, str] = field(default_factory=dict)        # chunk_id -> revision it was cut from
    converters: List[dict] = field(default_factory=list)
    not_indexed_sources: int = 0
    sources: int = 0
    params: dict = field(default_factory=dict)
    visibility_sha256: str = ""
    members_sha256: str = ""
    chunked_sources: int = 0           # sources whose chunks were computed in this assembly
    reused_sources: int = 0            # sources whose chunks came from the memo because nothing that feeds the chunker had changed
    repeated_chunks: int = 0           # chunks left out because an equal chunk (same units, same text) was already kept


def chunk_signature(unit_texts, params):
    """Everything the chunker reads: the displayed text of each unit, in order, with its heading path, and the chunking strategy and parameters.
    Equal signatures give equal chunks, so a source with an unchanged signature need not be chunked again (T-INC1)."""
    return ids.keyed_sha256(canonical_bytes({"units": [[uv, text, list(path)] for uv, text, path in unit_texts],
                                             "chunker": f"{chunker.NAME}@{chunker.VERSION}", "params": chunker.params_hash(params)}))


def _memo_get(conn, signature):
    row = conn.execute("SELECT chunk_ids_json FROM chunk_memo WHERE signature = ?", (signature,)).fetchone()
    if row is None:
        return None
    out = []
    for cid in json.loads(row[0]):
        c = conn.execute("SELECT unit_version_ids_json, text FROM chunks WHERE chunk_id = ?", (cid,)).fetchone()
        if c is None:
            return None                                   # a memo entry whose chunk rows are gone is ignored, never trusted
        out.append(chunker.Chunk(tuple(json.loads(c[0])), c[1], cid))
    return out


def _memo_put(conn, signature, chunks, params):
    for ch in chunks:                                     # the rows a later reuse reads must exist whatever happens to this commit
        conn.execute("INSERT OR IGNORE INTO chunks(chunk_id, unit_version_ids_json, text, strategy, params_hash) VALUES (?,?,?,?,?)",
                     (ch.chunk_id, json.dumps(list(ch.unit_version_ids)), ch.text, f"{chunker.NAME}@{chunker.VERSION}", chunker.params_hash(params)))
    conn.execute("INSERT OR REPLACE INTO chunk_memo(signature, chunk_ids_json) VALUES (?,?)", (signature, json.dumps([c.chunk_id for c in chunks])))


def visibility_input_sha256(conn):
    """Everything that decides who may see what: each collection's profile and every tombstone. Recomputed at verification."""
    profs = {cid: hashlib.sha256(pj.encode("utf-8")).hexdigest() for cid, pj in conn.execute("SELECT collection_id, profile_json FROM collections")}
    tombs = sorted(f"{t}:{lvl}" for t, lvl in conn.execute("SELECT target_id, level FROM tombstones"))
    return hashlib.sha256(canonical_bytes({"profiles": profs, "tombstones": tombs})).hexdigest()


def included_sources(conn):
    """Sources that take part in a commit: not hidden or removed by a tombstone (levels a and b)."""
    return conn.execute(
        "SELECT s.source_id, s.collection_id FROM sources s WHERE NOT EXISTS "
        "(SELECT 1 FROM tombstones t WHERE t.target_id = s.source_id AND t.level IN ('a','b')) ORDER BY s.source_id").fetchall()


def current_conversion(conn, source_id):
    row = conn.execute(
        "SELECT c.conv_id, c.converter, c.version, c.params_hash, r.revision_id FROM revision_observations o JOIN revisions r ON r.revision_id = o.revision_id "
        "JOIN conversions c ON c.revision_id = r.revision_id WHERE r.source_id = ? AND c.status IN ('ok','partial') "
        "AND o.obs_id = (SELECT max(o2.obs_id) FROM revision_observations o2 JOIN revisions r2 ON r2.revision_id = o2.revision_id WHERE r2.source_id = ?) "
        "ORDER BY c.rowid DESC LIMIT 1", (source_id, source_id)).fetchone()
    return row


MULTI_VALUED = frozenset({"tag", "links_to", "links_to_source"})     # keys where several statements are all true at once


def approval_sets(conn):
    """Statement ids that carry an approval (statement -> proposal -> approval), and those whose approval had override_protected."""
    approved, override = set(), set()
    for sid, flag in conn.execute(
            "SELECT s.statement_id, max(a.override_protected) FROM statements s JOIN proposals p ON p.proposal_id = s.proposal_id "
            "JOIN approvals a ON a.proposal_id = p.proposal_id WHERE p.state IN ('approved', 'applied') "
            "AND json_extract(s.evidence_json, '$.proposal_id') = s.proposal_id "
            "AND ((s.origin = 'ai' AND p.kind = 'ai_edit') OR (s.origin = 'human' AND p.kind IN ('metadata', 'patch'))) GROUP BY s.statement_id"):
        approved.add(sid)
        if flag:
            override.add(sid)
    return frozenset(approved), frozenset(override)


def _subject_statements(conn, subject_id, conv_id, roles, only_key=None, skip_key=None, approved=frozenset(), override_ok=frozenset(),
                        visible_sources=None):
    """Apply the display rule to every (subject, key) group and record each counted statement's role.
    Statements made by a conversion only count while that conversion is the source's current one (an older conversion's title or tags are
    history, not competitors). Multi-valued keys (tags, links) group per value, so every tag stays displayed. Returns {key: displayed value}
    for single-valued keys."""
    groups, values = {}, {}
    for sid, origin, state, supersedes, key, value_json, evidence in conn.execute(
            "SELECT statement_id, origin, state, supersedes, key, value_json, evidence_json FROM statements WHERE subject_id = ?", (subject_id,)):
        if (only_key and key != only_key) or (skip_key and key == skip_key):
            continue
        if key == "links_to_source" and visible_sources is not None and json.loads(value_json)["ref"] not in visible_sources:
            continue                                       # an edge to a hidden or removed source must not survive in a commit
        ev_conv = json.loads(evidence).get("conv_id")
        if ev_conv is not None and ev_conv != conv_id:
            continue
        if origin in ("human", "ai") and sid not in approved:
            continue                                       # a proposal nobody approved is not part of any commit (it stays a proposal)
        gkey = (key, value_json) if key in MULTI_VALUED else (key, None)
        groups.setdefault(gkey, []).append({"statement_id": sid, "origin": origin, "state": state, "supersedes": supersedes})
        values[sid] = value_json
    shown_values = {}
    for (key, _), items in groups.items():
        shown, _alts = display.choose(items, approved, override_ok)
        for s in items:
            if s["state"] in ("candidate", "committed"):
                roles[s["statement_id"]] = "display" if s["statement_id"] == shown else "alternate"
        if shown and key not in MULTI_VALUED:
            shown_values[key] = json.loads(values[shown])
    return shown_values


def source_of(conn, subject_id):
    """The source a statement subject belongs to: a source is itself; a unit version belongs to the source whose conversion created it."""
    if subject_id.startswith("src_"):
        return subject_id
    row = conn.execute("SELECT r.source_id FROM unit_versions uv JOIN conversions c ON c.conv_id = uv.conv_id "
                       "JOIN revisions r ON r.revision_id = c.revision_id WHERE uv.unit_version_id = ?", (subject_id,)).fetchone()
    return row[0] if row else None


def statement_head(conn, subject_id, key):
    """The statement that is displayed right now for a single-valued (subject, key), or None. Uses the same display rule and the same
    approval sets as assembly, over the statements of the source's current conversion plus those that carry no conversion (human, AI)."""
    src = source_of(conn, subject_id)
    conv = current_conversion(conn, src) if src else None
    conv_id = conv[0] if conv else None
    approved, override_ok = approval_sets(conn)
    items = []
    for sid, origin, state, supersedes, evidence in conn.execute(
            "SELECT statement_id, origin, state, supersedes, evidence_json FROM statements WHERE subject_id = ? AND key = ?", (subject_id, key)):
        ev = json.loads(evidence).get("conv_id")
        if ev is not None and ev != conv_id:
            continue
        items.append({"statement_id": sid, "origin": origin, "state": state, "supersedes": supersedes})
    shown, _ = display.choose(items, approved, override_ok)
    return shown


def tombstone_level(conn, target_id):
    """'b' if any level-b tombstone covers the target, else 'a' if any level-a one does, else None."""
    levels = {r[0] for r in conn.execute("SELECT level FROM tombstones WHERE target_id = ?", (target_id,))}
    return "b" if "b" in levels else ("a" if "a" in levels else None)


def hidden_level(conn, subject_id):
    """The strictest tombstone level that covers a source, or a unit version's unit or source: 'b', 'a' or None."""
    found = set()
    src = source_of(conn, subject_id)
    if src:
        found.add(tombstone_level(conn, src))
    if subject_id.startswith("uv_"):
        row = conn.execute("SELECT unit_id FROM unit_versions WHERE unit_version_id = ?", (subject_id,)).fetchone()
        if row:
            found.add(tombstone_level(conn, row[0]))
    elif subject_id.startswith("unit_"):
        found.add(tombstone_level(conn, subject_id))
    return "b" if "b" in found else ("a" if "a" in found else None)


def is_hidden(conn, subject_id):
    """True at any level: nothing may be proposed about it or sent to a model (level a is not shown to a proposer either)."""
    return hidden_level(conn, subject_id) is not None


def assemble(conn, params=None):
    params = params or chunker.DEFAULT_PARAMS
    asm = Assembly(params=dict(params))
    tombstoned_units = {t for (t,) in conn.execute("SELECT target_id FROM tombstones WHERE level IN ('a','b')")}
    seen_conv = {}
    all_uv, stmt_roles = set(), {}
    approved, override_ok = approval_sets(conn)
    included = included_sources(conn)
    visible = {sid for sid, _ in included}
    ctx = {"approved": approved, "override_ok": override_ok, "visible_sources": visible}
    for source_id, collection_id in included:
        asm.sources += 1
        conv = current_conversion(conn, source_id)
        if conv is None:
            _subject_statements(conn, source_id, None, stmt_roles, **ctx)
            continue
        conv_id, cname, cver, cparams, revision_id = conv
        _subject_statements(conn, source_id, conv_id, stmt_roles, **ctx)
        seen_conv[(cname, cver, cparams)] = {"converter": cname, "version": cver, "params_hash": cparams}
        rows = conn.execute("SELECT cu.ordinal, cu.unit_version_id, cu.locator_json FROM conversion_units cu WHERE cu.conv_id = ? "
                            "ORDER BY cu.ordinal", (conv_id,)).fetchall()
        unit_texts = []
        for _, uv, loc in rows:
            unit_id = conn.execute("SELECT unit_id FROM unit_versions WHERE unit_version_id = ?", (uv,)).fetchone()[0]
            if unit_id in tombstoned_units:
                continue
            all_uv.add(uv)
            shown = _subject_statements(conn, uv, conv_id, stmt_roles, **ctx)
            if "text" in shown:
                unit_texts.append((uv, shown["text"], tuple(json.loads(loc).get("heading", []))))
        try:
            profile.require_index(colls.get_profile(conn, collection_id))
        except PolicyViolation:
            asm.not_indexed_sources += 1
            continue
        sig = chunk_signature(unit_texts, params)
        cut = _memo_get(conn, sig)
        if cut is None:
            cut = chunker.chunk_units(unit_texts, params)
            _memo_put(conn, sig, cut, params)
            asm.chunked_sources += 1
        else:
            asm.reused_sources += 1
        for ch in cut:
            if ch.chunk_id in asm.chunk_source:                    # equal windows of one repetitive unit are one chunk: its id is its content
                asm.repeated_chunks += 1
                continue
            asm.chunks.append(ch)
            asm.chunk_source[ch.chunk_id] = source_id
            asm.chunk_revision[ch.chunk_id] = revision_id
    asm.unit_versions = sorted(all_uv)
    asm.statements = stmt_roles
    asm.converters = sorted(seen_conv.values(), key=lambda d: (d["converter"], d["version"], d["params_hash"]))
    asm.visibility_sha256 = visibility_input_sha256(conn)
    asm.members_sha256 = hashlib.sha256(canonical_bytes({
        "units": asm.unit_versions, "statements": sorted(stmt_roles.items()), "chunks": sorted(c.chunk_id for c in asm.chunks),
        "params": chunker.params_hash(params), "tokenizer": tk.TOKENIZER_ID, "visibility": asm.visibility_sha256, "format": FORMAT})).hexdigest()
    return asm


# ------------------------------------------------------------------------------------------------ paths and state
def staging_dir(home, commit_id):
    return os.path.join(home.sub("staging"), commit_id)


def segment_dir(home, commit_id):
    return os.path.join(home.sub("segments"), commit_id)


def active_commit_id(conn):
    row = conn.execute("SELECT commit_id FROM active_commit WHERE id = 1").fetchone()
    return row[0] if row else None


def read_manifest(directory):
    with open(os.path.join(directory, MANIFEST), "rb") as f:
        raw = f.read()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def _set_state(conn, commit_id, state, **extra):
    sets = ", ".join(["state = ?"] + [f"{k} = ?" for k in extra])
    conn.execute(f"UPDATE commits SET {sets} WHERE commit_id = ?", (state, *extra.values(), commit_id))


def abandon(home, conn, commit_id, why=""):
    if conn.in_transaction:
        conn.execute("ROLLBACK")
    row = conn.execute("SELECT state FROM commits WHERE commit_id = ?", (commit_id,)).fetchone()
    if row and row[0] in ("staging", "verified", "publishing"):
        _set_state(conn, commit_id, "abandoned")
    shutil.rmtree(staging_dir(home, commit_id), ignore_errors=True)


# ------------------------------------------------------------------------------------------------ ghost check
def ghost_check(conn, commit_id, directory, manifest):
    """Index content must equal the commit's members, in both directions, for every chunk. Returns a list of problem strings."""
    problems = []
    expected = {cid: text for cid, text in conn.execute(
        "SELECT c.chunk_id, c.text FROM commit_chunks cc JOIN chunks c ON c.chunk_id = cc.chunk_id WHERE cc.commit_id = ?", (commit_id,))}
    rows = ftsindex.read_map(os.path.join(directory, FTS))
    in_index = {r[1] for r in rows}
    in_manifest = set(manifest["members"]["chunks"])
    for label, a, b in (("index", in_index, set(expected)), ("manifest", in_manifest, set(expected))):
        if a - b:
            problems.append(f"ghost chunks in {label}: {len(a - b)}")
        if b - a:
            problems.append(f"chunks missing from {label}: {len(b - a)}")
    for _, chunk_id, source_id, sha, tokens in rows:
        text = expected.get(chunk_id)
        if text is None:
            continue
        if ids.text_sha256(text) != sha or manifest["chunks"].get(chunk_id, {}).get("text_sha256") != sha:
            problems.append(f"text hash differs for {chunk_id}")
        if ftsindex.token_string(text) != tokens:
            problems.append(f"index tokens differ for {chunk_id}")
        if manifest["chunks"].get(chunk_id, {}).get("source_id") != source_id:
            problems.append(f"source differs for {chunk_id}")
    # membership: everything the manifest promises about units, statements and roles must be what the database holds
    db_units = sorted(r[0] for r in conn.execute("SELECT unit_version_id FROM commit_units WHERE commit_id = ?", (commit_id,)))
    db_roles = {sid: role for sid, role in conn.execute("SELECT statement_id, role FROM commit_statements WHERE commit_id = ?", (commit_id,))}
    if db_units != manifest["members"]["unit_versions"]:
        problems.append("unit versions differ between the database and the manifest")
    if db_roles != manifest["members"]["statements"]:
        problems.append("statements or their display roles differ between the database and the manifest")
    recomputed = hashlib.sha256(canonical_bytes({
        "units": db_units, "statements": sorted(db_roles.items()), "chunks": sorted(expected), "params": manifest["index"]["params_hash"],
        "tokenizer": manifest["index"]["tokenizer"], "visibility": manifest["visibility_input_sha256"], "format": manifest["format"]})).hexdigest()
    if recomputed != manifest["members_sha256"]:
        problems.append("members commitment differs from what the database holds")
    # every chunk's id must be reproducible from its own members, text and the committed parameters
    name, _, version = manifest["index"]["chunker"].partition("@")
    for chunk_id, uvs_json, text in conn.execute(
            "SELECT c.chunk_id, c.unit_version_ids_json, c.text FROM commit_chunks cc JOIN chunks c ON c.chunk_id = cc.chunk_id WHERE cc.commit_id = ?",
            (commit_id,)):
        try:
            again = ids.chunk_id(json.loads(uvs_json), name, version, manifest["index"]["params_hash"], ids.text_sha256(text))
        except KbError:
            again = None
        if again != chunk_id:
            problems.append(f"chunk payload does not match its id for {chunk_id}")
    if manifest["index"]["tokenizer"] != tk.TOKENIZER_ID:
        problems.append("tokenizer version differs")
    if manifest["visibility_input_sha256"] != visibility_input_sha256(conn):
        problems.append("visibility inputs changed since assembly")
    return problems


# ------------------------------------------------------------------------------------------------ build and publish
@dataclass
class BuildResult:
    commit_id: str
    outcome: str                      # created | unchanged
    chunks: int = 0
    sources: int = 0
    chunked: int = 0                  # sources chunked from scratch in this build
    reused: int = 0                   # sources whose chunks were reused (nothing feeding the chunker had changed)
    pruned: int = 0                   # older versions removed because they still held content that is hidden now
    erasure_pending: List[str] = field(default_factory=list)       # steps of the hide promise that did not finish (see `erasure_pending`)


def _stage_members(conn, commit_id, asm):
    conn.execute("BEGIN IMMEDIATE")
    try:
        for ch in asm.chunks:
            conn.execute("INSERT OR IGNORE INTO chunks(chunk_id, unit_version_ids_json, text, strategy, params_hash) VALUES (?,?,?,?,?)",
                         (ch.chunk_id, json.dumps(list(ch.unit_version_ids)), ch.text, f"{chunker.NAME}@{chunker.VERSION}",
                          chunker.params_hash(asm.params)))
        conn.executemany("INSERT INTO commit_units(commit_id, unit_version_id) VALUES (?,?)", [(commit_id, uv) for uv in asm.unit_versions])
        conn.executemany("INSERT INTO commit_statements(commit_id, statement_id, role) VALUES (?,?,?)",
                         [(commit_id, sid, role) for sid, role in sorted(asm.statements.items())])
        conn.executemany("INSERT INTO commit_chunks(commit_id, chunk_id) VALUES (?,?)", [(commit_id, c.chunk_id) for c in asm.chunks])
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _write_artifacts(directory, commit_id, parent, asm, extensions=()):
    """Write the lexical index and every optional artifact (vectors) into the staging folder and the manifest that binds them together."""
    os.makedirs(directory)
    fts_path = os.path.join(directory, FTS)
    ftsindex.build(fts_path, [(c.chunk_id, asm.chunk_source[c.chunk_id], c.text, ids.text_sha256(c.text)) for c in asm.chunks])
    fd = os.open(fts_path, os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    artifacts = {FTS: {"sha256": _sha_file(fts_path), "size": os.path.getsize(fts_path)}}
    described = {}
    for ext in extensions:
        meta = ext.write(directory, asm)
        artifacts[ext.name] = {"sha256": _sha_file(os.path.join(directory, ext.name)), "size": os.path.getsize(os.path.join(directory, ext.name))}
        described[ext.name] = {"signature": ext.signature(), "meta": meta}
    manifest = {
        "format": FORMAT, "commit_id": commit_id, "parent_commit_id": parent, "created_at": _now(),
        "members": {"unit_versions": asm.unit_versions, "statements": dict(sorted(asm.statements.items())), "chunks": sorted(c.chunk_id for c in asm.chunks)},
        "chunks": {c.chunk_id: {"text_sha256": ids.text_sha256(c.text), "source_id": asm.chunk_source[c.chunk_id],
                            "revision_id": asm.chunk_revision[c.chunk_id]} for c in asm.chunks},
        "index": {"kind": ftsindex.INDEX_KIND, "tokenizer": tk.TOKENIZER_ID, "chunker": f"{chunker.NAME}@{chunker.VERSION}",
                  "params": asm.params, "params_hash": chunker.params_hash(asm.params)},
        "artifacts": artifacts, "extensions": described,
        "visibility_input_sha256": asm.visibility_sha256, "members_sha256": asm.members_sha256, "converters": asm.converters,
        "counts": {"sources": asm.sources, "not_indexed_sources": asm.not_indexed_sources, "chunks": len(asm.chunks)},
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    _write_file(os.path.join(directory, MANIFEST), raw)
    fsync_dir(directory)
    return hashlib.sha256(raw).hexdigest()


def verify_artifacts(directory, manifest_sha=None):
    """Re-read and re-hash a folder of artifacts. Returns (manifest, problems)."""
    problems = []
    try:
        manifest, msha = read_manifest(directory)
    except (OSError, ValueError) as e:
        return None, [f"manifest unreadable: {e}"]
    if manifest_sha and msha != manifest_sha:
        problems.append("manifest hash differs from the one recorded")
    for name, meta in manifest["artifacts"].items():
        p = os.path.join(directory, name)
        if not os.path.isfile(p):
            problems.append(f"{name} missing")
        elif os.path.getsize(p) != meta["size"] or _sha_file(p) != meta["sha256"]:
            problems.append(f"{name} does not match the manifest")
    return manifest, problems


def _publish_rename(src, dst):
    last = None
    for attempt in range(RENAME_TRIES):
        try:
            _rename_dir(src, dst)
            return
        except PermissionError as e:               # something (antivirus, an indexer) holds a handle: wait and retry
            last = e
            time.sleep(0.05 * (2 ** attempt))
    raise CommitFailed(f"could not rename {src} to {dst}: {last}")


def _activate(conn, commit_id, manifest_sha, member_statements, visibility_sha):
    conn.execute("BEGIN IMMEDIATE")
    try:
        if visibility_sha != visibility_input_sha256(conn):
            raise CommitFailed("a profile or tombstone changed after this commit was assembled; it was not activated")
        old = active_commit_id(conn)
        if old:
            _set_state(conn, old, "retired")
        cur = conn.execute("UPDATE commits SET state='active', manifest_sha256=? WHERE commit_id=? AND state='publishing'", (manifest_sha, commit_id))
        if cur.rowcount != 1:
            raise CommitFailed(f"commit {commit_id} is not in the publishing state")
        conn.execute("INSERT OR REPLACE INTO active_commit(id, commit_id) VALUES (1, ?)", (commit_id,))
        ids_ = list(member_statements)
        for i in range(0, len(ids_), 500):
            part = ids_[i:i + 500]
            conn.execute(f"UPDATE statements SET state='committed' WHERE state='candidate' AND statement_id IN ({','.join('?' * len(part))})", part)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def build(home, conn, params=None, force=False, extensions=()):
    """Assemble, verify and publish a commit of everything currently known. Returns a BuildResult; the caller holds the writer lock.
    `extensions` are optional artifacts (the vector file): each one prepares itself BEFORE anything is staged (so a refusal changes nothing),
    writes its file into the staging folder, and is checked against the members together with the lexical index; its description goes into the
    manifest, and a commit whose extensions differ from the active one is never 'unchanged'."""
    asm = assemble(conn, params)
    parent = active_commit_id(conn)
    wanted = {e.name: e.signature() for e in extensions}
    for ext in extensions:
        ext.prepare(conn, asm)
    if parent and not force:
        try:
            old_manifest, _ = read_manifest(segment_dir(home, parent))
            have = {n: d["signature"] for n, d in old_manifest.get("extensions", {}).items()}
            if old_manifest.get("members_sha256") == asm.members_sha256 and have == wanted:
                pruned, pending = _after_publish(home, conn, asm, extensions)       # a build that was cut off after activation is finished by the next one
                return BuildResult(parent, "unchanged", len(asm.chunks), asm.sources, asm.chunked_sources, asm.reused_sources, len(pruned), pending)
        except (OSError, ValueError):
            pass                                    # the active commit's files are unreadable: build a new one
    commit_id = ids.new_id("cm")
    conn.execute("INSERT INTO commits(commit_id, parent_commit_id, state, created_at) VALUES (?,?, 'staging', ?)", (commit_id, parent, _now()))
    fault_point("commit.begin")
    sdir = staging_dir(home, commit_id)
    try:
        _stage_members(conn, commit_id, asm)
        manifest_sha = _write_artifacts(sdir, commit_id, parent, asm, extensions)
        fault_point("commit.after_artifacts")
        manifest, problems = verify_artifacts(sdir, manifest_sha)
        if not problems:
            problems = ghost_check(conn, commit_id, sdir, manifest)
        for ext in extensions:
            if not problems:
                problems = ext.check(sdir, manifest, asm)
        if problems:
            raise CommitFailed("verification failed: " + "; ".join(problems))
        _set_state(conn, commit_id, "verified", manifest_sha256=manifest_sha)
        fault_point("commit.after_verify")
        _set_state(conn, commit_id, "publishing")
        fault_point("commit.after_journal")
        _publish(home, conn, commit_id, manifest_sha, asm.statements)
    except KbError:
        abandon(home, conn, commit_id)
        raise
    except (OSError, sqlite3.Error) as e:
        abandon(home, conn, commit_id)
        raise CommitFailed(f"commit {commit_id} failed and was abandoned: {e}") from e
    pruned, pending = _after_publish(home, conn, asm, extensions)
    return BuildResult(commit_id, "created", len(asm.chunks), asm.sources, asm.chunked_sources, asm.reused_sources, len(pruned), pending)


def _after_publish(home, conn, asm, extensions):
    """What follows an activation: drop from the vector cache what only an older commit held, remove retired commits built under visibility rules
    that no longer hold, remove opened copies of hidden sources. Each step depends only on the active state, so it can be repeated at any time;
    `build` runs it even when nothing changed, which is how a process that died between the activation and this point is made good."""
    for ext in extensions:
        ext.finish(conn, asm)
    pruned = prune_stale_history(home, conn)
    purge_opened(home, conn)
    purge_ask_records(home, conn)
    scrub_hidden_text(conn)
    compact_after_scrub(conn)
    purge_old_backups(home, conn)
    purge_ask_tickets(home, conn)
    return pruned, erasure_pending(home, conn)


def _stale_segments(home, conn):
    """[(commit_id, folder)] of retired commits whose files still exist although they must not: built under visibility rules that no longer hold
    (something was hidden or a profile changed since), or unreadable while something is hidden (it can neither be rolled back to nor shown to be free of
    hidden text; with nothing hidden the damage is doctor's business and the folder is left alone)."""
    current, anything_hidden, out = visibility_input_sha256(conn), _newest_hide(conn) is not None, []
    for (cid,) in conn.execute("SELECT commit_id FROM commits WHERE state = 'retired'").fetchall():
        d = segment_dir(home, cid)
        if not os.path.isdir(d):
            continue
        try:
            manifest, _ = read_manifest(d)
        except (OSError, ValueError):
            if anything_hidden:
                out.append((cid, d))
            continue
        if manifest.get("visibility_input_sha256") != current:
            out.append((cid, d))
    return out


def prune_stale_history(home, conn):
    """Remove the files of retired commits that were assembled under visibility rules that no longer hold (something was hidden or a profile
    changed since). Rolling back to such a commit would be refused anyway; keeping its index on disk would keep hidden text findable (Gate 1 H2).
    Returns the commit ids whose folders are gone afterwards (a folder that could not be removed is not counted)."""
    removed = []
    for cid, d in _stale_segments(home, conn):
        shutil.rmtree(d, ignore_errors=True)
        if not os.path.isdir(d):
            removed.append(cid)
    if removed:
        audit.append(conn, "history_pruned", {"count": len(removed)})
    return removed


def _opened_to_remove(home, conn):
    """Folders of <home>/opened (named copies made by `kb open`) whose source is hidden or unknown."""
    root = os.path.join(home.path, "opened")
    if not os.path.isdir(root):
        return []
    out = []
    for prefix in os.listdir(root):
        sources = [r[0] for r in conn.execute("SELECT DISTINCT source_id FROM revisions WHERE substr(blob_sha256, 1, ?) = ?", (len(prefix), prefix))]
        if not sources or any(is_hidden(conn, s) for s in sources):
            out.append(os.path.join(root, prefix))
    return out


def purge_opened(home, conn):
    """`kb open` writes a named copy of a stored original under <home>/opened/<hash prefix>/. It is a cache, but it holds the original's bytes, so
    a copy whose source has been hidden (level a or b) is removed at the next commit instead of staying behind as one more place that holds the
    text. A copy that a viewer still has open cannot be removed on Windows; it is left, `erasure_pending` says so, and the next commit tries again.
    Returns the number removed."""
    removed = 0
    for path in _opened_to_remove(home, conn):
        shutil.rmtree(path, ignore_errors=True)
        removed += 0 if os.path.exists(path) else 1
    return removed


_BATCH = 400
ERASED_VALUE, ERASED_EVIDENCE = '""', "{}"


def _rows_in(conn, sql, values):
    """Rows of `sql` (which holds one `{marks}` for an IN list) for every value, a few hundred at a time (SQLite limits the number of variables)."""
    values = sorted(values)
    for i in range(0, len(values), _BATCH):
        part = values[i:i + _BATCH]
        for row in conn.execute(sql.format(marks=",".join("?" * len(part))), part):
            yield row


PURGED_DIFF = '{"purged": true}'
_NAMING_KEYS = ("subject_id", "unit_version_id", "unit_id", "target_id")


def _ids_named(diff):
    """Every source, unit or unit-version identifier that a proposal's difference names."""
    found, stack = set(), [diff]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            for k, v in x.items():
                if k in _NAMING_KEYS and isinstance(v, str):
                    found.add(v)
                stack.append(v)
        elif isinstance(x, list):
            stack.extend(x)
    return found


def _scrub_proposals(conn, hidden_ids):
    """Blank the difference of every proposal (except a delete, which holds only an identifier and a level and is the permission for its own tombstone)
    that names something hidden. A pending or approved one can no longer be carried out and becomes stale; that happens first, and it is what makes the proposal trigger
    accept the blanking (it allows it for a decided proposal), so no schema change was needed."""
    n, now = 0, _now()
    for pid, state, diff_json in conn.execute("SELECT proposal_id, state, diff_json FROM proposals WHERE kind != 'delete' AND diff_json != ?",
                                              (PURGED_DIFF,)).fetchall():
        try:
            named = _ids_named(json.loads(diff_json))
        except ValueError:
            continue
        if named & hidden_ids:
            if state in ("pending", "approved"):
                conn.execute("UPDATE proposals SET state = 'stale', decided_at = ? WHERE proposal_id = ?", (now, pid))
            conn.execute("UPDATE proposals SET diff_json = ? WHERE proposal_id = ?", (PURGED_DIFF, pid))
            n += 1
    return n


def scrub_hidden_text(conn):
    """Gate 1 H2: once something is hidden at level a or b, the TEXT derived from it is erased from the database too, not only from the index. Erased: the
    values and evidence of every statement about the hidden source or unit (and about their unit versions), the text of every chunk that was cut from one of
    their unit versions, the payload of patches on their units, and the difference of every proposal that names any of them. What stays on purpose: the original blob (Gate 1 decision 4), and ids, kinds, locators and
    hashes. A tombstone is permanent (nothing in kb ends a hide), so nothing here takes away something that could come back. The erasure is one transaction and
    runs inside the scrub window that the statement trigger needs; it is repeated, harmlessly, after every commit. Returns {"statements": n, "chunks": n,
    "patches": n, "proposals": n, "locators": n} of the rows that changed (a locator is where a unit stood: its headings and its offsets in the file). The bytes of the old text stay in the database file until it is compacted
    (`compact_after_scrub`)."""
    if conn.execute("SELECT 1 FROM tombstones WHERE level IN ('a', 'b') LIMIT 1").fetchone() is None:
        return {"statements": 0, "chunks": 0, "patches": 0, "proposals": 0, "locators": 0}      # nothing hidden: no write transaction is taken
    conn.execute("BEGIN IMMEDIATE")
    try:
        sources, units = set(), set()
        for (target,) in conn.execute("SELECT target_id FROM tombstones WHERE level IN ('a', 'b')"):
            (sources if target.startswith("src_") else units).add(target)
        done = {"statements": 0, "chunks": 0, "patches": 0, "proposals": 0, "locators": 0}
        uvs, all_units = set(), set(units)
        for uv, unit in _rows_in(conn, "SELECT uv.unit_version_id, uv.unit_id FROM unit_versions uv JOIN conversions c ON c.conv_id = uv.conv_id "
                                       "JOIN revisions r ON r.revision_id = c.revision_id WHERE r.source_id IN ({marks})", sources):
            uvs.add(uv)
            all_units.add(unit)
        for uv, unit in _rows_in(conn, "SELECT unit_version_id, unit_id FROM unit_versions WHERE unit_id IN ({marks})", units):
            uvs.add(uv)
            all_units.add(unit)
        subjects = sources | units | uvs
        conn.execute("INSERT INTO scrub_window(id) VALUES (1)")
        for part in range(0, len(subjects), _BATCH):
            batch = sorted(subjects)[part:part + _BATCH]
            done["statements"] += conn.execute(
                f"UPDATE statements SET value_json = ?, evidence_json = ? WHERE subject_id IN ({','.join('?' * len(batch))}) "
                "AND (value_json != ? OR evidence_json != ?)", [ERASED_VALUE, ERASED_EVIDENCE, *batch, ERASED_VALUE, ERASED_EVIDENCE]).rowcount
        done["proposals"] = _scrub_proposals(conn, subjects | all_units)
        for part in range(0, len(uvs), _BATCH):                       # the headings above a hidden unit and where it stood in the file (ninth review)
            batch = sorted(uvs)[part:part + _BATCH]
            done["locators"] += conn.execute(
                f"UPDATE conversion_units SET locator_json = '{{}}' WHERE unit_version_id IN ({','.join('?' * len(batch))}) AND locator_json != '{{}}'",
                batch).rowcount
        conn.execute("DELETE FROM scrub_window")
        wipe = [cid for cid, uv_json in conn.execute("SELECT chunk_id, unit_version_ids_json FROM chunks WHERE text != ''")
                if uvs.intersection(json.loads(uv_json))]
        for part in range(0, len(wipe), _BATCH):
            batch = wipe[part:part + _BATCH]
            done["chunks"] += conn.execute(f"UPDATE chunks SET text = '' WHERE chunk_id IN ({','.join('?' * len(batch))})", batch).rowcount
        for part in range(0, len(all_units), _BATCH):
            batch = sorted(all_units)[part:part + _BATCH]
            done["patches"] += conn.execute(
                f"UPDATE patches SET anchor_json = '{{}}', payload_json = '{{}}' WHERE unit_id IN ({','.join('?' * len(batch))}) "
                "AND (anchor_json != '{}' OR payload_json != '{}')", batch).rowcount
        if any(done.values()):
            audit.append(conn, "text_scrubbed", done)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return done


def revision_holds_hidden_unit(conn, revision_id):
    """True when a unit that is hidden (level a or b) has a version in a conversion of this revision. A converter upgrade does not convert such a
    revision again: a unit keeps its id only while its structural key is the same, so a new converter could write the hidden text under an id that no
    tombstone names (sixth review, 2026-11-23)."""
    return conn.execute(
        "SELECT 1 FROM tombstones t JOIN unit_versions uv ON uv.unit_id = t.target_id JOIN conversions c ON c.conv_id = uv.conv_id "
        "WHERE t.level IN ('a', 'b') AND c.revision_id = ? LIMIT 1", (revision_id,)).fetchone() is not None


def compact_after_scrub(conn):
    """SQLite does not overwrite what an UPDATE replaces: the old text stays in free pages of the database file, and in the write-ahead log until a
    checkpoint (measured, second review of S4, 2026-11-20: after a hide and a commit the text was still found byte by byte in the file and in the log).
    After a scrub that changed rows, VACUUM rewrites the file and a truncating checkpoint empties the log. Repeatable: it runs when the newest
    `text_scrubbed` audit event is newer than the newest `db_compacted` one, so a failure (disk full, a reader that blocks the checkpoint) is retried
    at the next commit and recorded as `compaction_failed`. Returns True when it ran to the end."""
    last_scrub = conn.execute("SELECT max(event_id) FROM audit_events WHERE type = 'text_scrubbed'").fetchone()[0]
    if last_scrub is None:
        return False
    if last_scrub <= (conn.execute("SELECT max(event_id) FROM audit_events WHERE type = 'db_compacted'").fetchone()[0] or 0):
        return False
    try:
        conn.execute("VACUUM")
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy and busy[0]:
            raise sqlite3.OperationalError("the log could not be emptied: another connection holds it")
    except sqlite3.Error:
        audit.append(conn, "compaction_failed", {"count": 1})
        return False
    audit.append(conn, "db_compacted", {"count": 1})
    return True


def _newest_hide(conn):
    """Time (seconds since the epoch) of the newest level a or b tombstone, None when nothing is hidden. A tombstone whose time cannot be read counts as
    in the future, so that everything is older than it."""
    newest = None
    for (at,) in conn.execute("SELECT at FROM tombstones WHERE level IN ('a', 'b')"):
        try:
            stamp = float(calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ")))
        except (ValueError, TypeError):
            stamp = float("inf")
        newest = stamp if newest is None else max(newest, stamp)
    return newest


def _backups(home, conn):
    """Paths in <home>/backups once anything is hidden, otherwise none."""
    root = home.sub("backups")
    if _newest_hide(conn) is None or not os.path.isdir(root):
        return []
    return [os.path.join(root, name) for name in os.listdir(root)]


def purge_old_backups(home, conn):
    """A pre-migration backup is a copy of the whole database as it was. Once anything is hidden, every backup goes at the next commit: one made before
    the hide holds the text, and one made between the hide and the erasure holds it too (second review, 2026-11-20: the first version kept the newer
    ones). A backup only serves to undo a failed migration, and a commit runs on a database whose migration has already succeeded. With nothing hidden,
    backups are left alone. Returns the number of files removed."""
    removed = 0
    for path in _backups(home, conn):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


TICKET_DEAD_AFTER = 3600       # seconds; `ask_runner.MAX_TIMEOUT` is kept well below it
STALE_SUFFIX = ".stale"        # a file next to a working folder that `kb ask` could not remove at the end of its question (eighth review)


def _tickets_to_remove(home, conn):
    """Working folders of `kb ask` made before the newest hide (they may hold text that is hidden now; a question still running on one fails safely)
    or older than an hour (a question times out long before; its process is gone). The age is the folder's own modification time, which is set when
    the question starts (the harness writes into a sub-folder), so it is the start of the question."""
    root = os.path.join(home.path, "ask", "tickets")
    if not os.path.isdir(root):
        return []
    newest, out = _newest_hide(conn), []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if name.endswith(STALE_SUFFIX):               # `kb ask` could not remove this folder when its question ended: it is over, so it goes now
            folder = path[:-len(STALE_SUFFIX)]
            if os.path.isdir(folder):
                out.append(folder)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
            continue
        try:
            age_stamp = os.path.getmtime(path)
        except OSError:
            continue
        if age_stamp < time.time() - TICKET_DEAD_AFTER or (newest is not None and age_stamp < newest + 1):         # +1: the stamp of the hide has whole seconds
            out.append(path)
    return list(dict.fromkeys(out))


def purge_ask_tickets(home, conn):
    """Remove `_tickets_to_remove` (a crash leaves a ticket with the prompt text; a finished question removes its own). Returns the number removed."""
    removed = 0
    for path in _tickets_to_remove(home, conn):
        shutil.rmtree(path, ignore_errors=True)
        if os.path.exists(path):
            continue
        removed += 1
        try:
            os.remove(path + STALE_SUFFIX)            # the marker has done its work (a marker left behind is also removed by the next listing)
        except OSError:
            pass
    return removed


def passage_hidden(conn, passage):
    """A passage of a recorded answer is hidden when its source is, or when any unit it was cut from is."""
    if not isinstance(passage, dict):
        return False
    if isinstance(passage.get("source_id"), str):
        if is_hidden(conn, passage["source_id"]):
            return True
        row = conn.execute("SELECT c.profile_json FROM sources s JOIN collections c ON c.collection_id = s.collection_id WHERE s.source_id = ?",
                           (passage["source_id"],)).fetchone()
        if row is not None and json.loads(row[0]).get("agent_visible") is not True:          # the collection was closed to agents after the answer was made
            return True
    row = conn.execute("SELECT unit_version_ids_json FROM chunks WHERE chunk_id = ?", (passage.get("chunk_id"),)).fetchone()
    for uv in (json.loads(row[0]) if row else []):
        unit = conn.execute("SELECT unit_id FROM unit_versions WHERE unit_version_id = ?", (uv,)).fetchone()
        if unit and is_hidden(conn, unit[0]):
            return True
    return False


def _hidden_ask_records(home, conn):
    """Answer files of `kb ask` (path of the record) whose passages include text of something hidden."""
    root = os.path.join(home.path, "ask", "outbox")
    if not os.path.isdir(root):
        return []
    out = []
    for name in os.listdir(root):
        if not (name.startswith("ans-") and name.endswith(".json")) or name.endswith(".released.json"):
            continue
        path = os.path.join(root, name)
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
            passages = list(record.get("passages", [])) + list(record.get("flagged", []))
        except (OSError, ValueError, AttributeError, TypeError):
            continue                                              # a file that cannot be read is doctor's business
        if any(passage_hidden(conn, p) for p in passages):
            out.append(path)
    return out


def purge_ask_records(home, conn):
    """`kb ask` keeps each answer as a file under <home>/ask/outbox with the full text of the passages the model was given. Like an opened copy, a record
    that holds text of something hidden (a source, or a unit of it) is removed at the next commit, together with its release marker, instead of staying
    behind as one more place that holds the text. The format is read as plain JSON (this module is tier T0 and may not import the question layer).
    Returns the number of records removed."""
    removed = 0
    for path in _hidden_ask_records(home, conn):
        gone = True
        for victim in (path, path[:-len(".json")] + ".released.json"):
            try:
                os.remove(victim)
            except FileNotFoundError:
                pass
            except OSError:
                gone = False
        removed += 1 if gone else 0
    return removed


def erasure_pending(home, conn):
    """What the hide promise still lacks, read from the real state after the erasure steps ran (not from what the steps report): [] when nothing is hidden or
    everything is done, otherwise one sentence per step that did not finish. A step that fails (a file held open, no disk space, no permission) is retried at
    the next commit, but a commit that returns normally must not look like a finished erasure (third review, round 4)."""
    if _newest_hide(conn) is None:
        return []
    out = []
    last_scrub = conn.execute("SELECT max(event_id) FROM audit_events WHERE type = 'text_scrubbed'").fetchone()[0]
    last_compacted = conn.execute("SELECT max(event_id) FROM audit_events WHERE type = 'db_compacted'").fetchone()[0] or 0
    if last_scrub is not None and last_scrub > last_compacted:
        out.append("the database file may still hold the old text (the compaction did not finish)")
    for what, found in (("backup", _backups(home, conn)), ("older version", [c for c, _ in _stale_segments(home, conn)]),
                        ("opened copy", _opened_to_remove(home, conn)), ("recorded answer", _hidden_ask_records(home, conn)),
                        ("working folder", _tickets_to_remove(home, conn))):
        if found:
            out.append(f"{len(found)} {what}(s) with hidden text could not be removed")
    return out


def _publish(home, conn, commit_id, manifest_sha, member_statements):
    sdir, gdir = staging_dir(home, commit_id), segment_dir(home, commit_id)
    if not os.path.isdir(gdir):
        _publish_rename(sdir, gdir)
    fsync_dir(home.sub("segments"))
    fsync_dir(home.sub("staging"))
    fault_point("commit.after_rename")
    manifest, problems = verify_artifacts(gdir, manifest_sha)          # read back from the final location
    if problems:
        raise CommitFailed("read-back after publishing failed: " + "; ".join(problems))
    fault_point("commit.after_readback")
    fault_point("commit.before_activate")
    _activate(conn, commit_id, manifest_sha, member_statements, manifest["visibility_input_sha256"])
    fault_point("commit.after_activate")


# ------------------------------------------------------------------------------------------------ recovery, serving, rollback, gc
def _member_statements(conn, commit_id):
    return [r[0] for r in conn.execute("SELECT statement_id FROM commit_statements WHERE commit_id = ?", (commit_id,))]


def recover(home, conn):
    """Startup recovery (the caller holds the writer lock). Returns a list of (commit_id, action)."""
    actions = []
    for commit_id, state, msha in conn.execute("SELECT commit_id, state, manifest_sha256 FROM commits WHERE state IN ('staging','verified','publishing')").fetchall():
        sdir, gdir = staging_dir(home, commit_id), segment_dir(home, commit_id)
        if state in ("staging", "verified"):
            abandon(home, conn, commit_id)
            actions.append((commit_id, "abandoned"))
            continue
        # publishing: finish it if its artifacts can be found and verified, otherwise abandon and keep the previous active commit
        done = False
        for place in (gdir, sdir):
            if os.path.isdir(place):
                manifest, problems = verify_artifacts(place, msha)
                if not problems:
                    problems = ghost_check(conn, commit_id, place, manifest)        # content, membership and current visibility rules
                if not problems:
                    try:
                        if place == sdir:
                            if os.path.isdir(gdir):
                                shutil.rmtree(gdir)
                            _publish_rename(sdir, gdir)
                            fsync_dir(home.sub("segments"))
                        _activate(conn, commit_id, msha, _member_statements(conn, commit_id), manifest["visibility_input_sha256"])
                        actions.append((commit_id, "completed"))
                        done = True
                    except KbError:
                        done = False
                    break
        if not done:
            abandon(home, conn, commit_id)
            shutil.rmtree(gdir, ignore_errors=True)
            actions.append((commit_id, "abandoned"))
    # the active commit whose rename did not survive a crash: the staging folder may still hold it
    active = active_commit_id(conn)
    if active and not os.path.isdir(segment_dir(home, active)) and os.path.isdir(staging_dir(home, active)):
        _, problems = verify_artifacts(staging_dir(home, active))
        if not problems:
            _publish_rename(staging_dir(home, active), segment_dir(home, active))
            fsync_dir(home.sub("segments"))
            actions.append((active, "re-published"))
    return actions


def active_artifacts(home, conn):
    """(commit_id, manifest, fts path) of the active commit after a cheap check (files exist, sizes match). Raises ArtifactsMissing."""
    cid = active_commit_id(conn)
    if cid is None:
        raise ArtifactsMissing("there is no active commit yet; run kb commit")
    d = segment_dir(home, cid)
    try:
        manifest, _ = read_manifest(d)
        for name, meta in manifest["artifacts"].items():
            if os.path.getsize(os.path.join(d, name)) != meta["size"]:
                raise ArtifactsMissing(f"{name} of commit {cid} has the wrong size")
    except (OSError, ValueError) as e:
        raise ArtifactsMissing(f"the files of the active commit {cid} are missing or damaged ({e}); run kb rollback") from None
    return cid, manifest, os.path.join(d, FTS)


def rollback(home, conn):
    """Switch back to the previous commit if its files verify. Never invents anything: it only returns to a published, verified version."""
    cid = active_commit_id(conn)
    if cid is None:
        raise CommitFailed("nothing to roll back")
    parent = conn.execute("SELECT parent_commit_id FROM commits WHERE commit_id = ?", (cid,)).fetchone()[0]
    if parent is None or conn.execute("SELECT state FROM commits WHERE commit_id = ?", (parent,)).fetchone()[0] != "retired":
        raise CommitFailed("there is no previous commit to return to")
    manifest, problems = verify_artifacts(segment_dir(home, parent))
    if problems:
        raise CommitFailed("the previous commit's files do not verify: " + "; ".join(problems))
    if manifest["visibility_input_sha256"] != visibility_input_sha256(conn):          # the same rule that refuses to activate a commit assembled under old rules
        raise CommitFailed("the previous commit was assembled under visibility rules that no longer hold (something was hidden or a profile changed "
                           "since); it was not restored")
    conn.execute("BEGIN IMMEDIATE")
    try:
        _set_state(conn, cid, "retired")
        _set_state(conn, parent, "active")
        conn.execute("UPDATE active_commit SET commit_id = ? WHERE id = 1", (parent,))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return parent


def gc(home, conn, keep_retired=2):
    """Remove derived folders nobody needs: abandoned or unknown staging folders and segments of old retired commits. Returns removed names."""
    removed = []
    live_staging = {r[0] for r in conn.execute("SELECT commit_id FROM commits WHERE state IN ('staging','verified','publishing')")}
    for name in os.listdir(home.sub("staging")):
        if name not in live_staging:
            shutil.rmtree(os.path.join(home.sub("staging"), name), ignore_errors=True)
            removed.append("staging/" + name)
    keep = {r[0] for r in conn.execute("SELECT commit_id FROM commits WHERE state IN ('active','publishing')")}
    keep |= {r[0] for r in conn.execute("SELECT commit_id FROM commits WHERE state='retired' ORDER BY created_at DESC, rowid DESC LIMIT ?", (keep_retired,))}
    for name in os.listdir(home.sub("segments")):
        if name not in keep:
            shutil.rmtree(os.path.join(home.sub("segments"), name), ignore_errors=True)
            removed.append("segments/" + name)
    return removed
