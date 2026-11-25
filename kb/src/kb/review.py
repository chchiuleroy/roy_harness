"""Proposals, approvals and what applying one does (Gate 2 section 4.9; Gate 1 decisions 3, 4 and 6).

    create   a proposal is a request for a change plus the exact difference (kind, diff, who asked, which commit it was made against)
    approve  only the user's approval makes a proposal applicable; a proposal made against an older commit is marked stale instead
    apply    one database transaction writes the change (statements, a tombstone, a patch); nothing becomes visible until the next commit
    reject   closes a pending proposal
    purge_expired  decided proposals keep their diff (which may contain original text) for a limited time only

Rules the database also enforces by trigger (schema migration 4), so that a bug here cannot skip them: a tombstone needs an approved delete
proposal, an approval needs a pending proposal, a proposal's state moves along legal paths only, and a human or AI statement is committed only
with an approved proposal behind it. Original evidence (blobs) and conversion outputs are never touched: a correction is a new statement.
Hiding (levels a and b) removes content from search and general access; it keeps the original blob (Gate 1 decision 4). Level c (physical
erasure) and 'purge' are refused until Roy decides the policy; 'merge_tag' is not implemented in S2.
"""
import calendar
import json
import time
from contextlib import contextmanager

from . import audit, commit, evidence, ids, patches
from .errors import ReviewRefused, StaleProposal

KINDS = ("ai_edit", "metadata", "merge_tag", "delete", "purge", "patch")
SUPPORTED = frozenset({"ai_edit", "metadata", "delete", "patch"})
STATEMENT_KEYS = frozenset({"title", "tag", "text", "summary", "language", "note"})
FACTUAL_KEYS = frozenset({"text", "summary", "note"})     # an AI edit that writes any of these must cite evidence (a retitle or a tag need not)
MULTI_VALUED_KEYS = frozenset({"tag"})
LEVELS = ("a", "b")
MAX_STATEMENTS, MAX_VALUE = 50, 20000
RETENTION_DAYS = 30          # decided proposals keep their diff this many days, then it is blanked (a proposal's diff may hold original text)
PURGED = '{"purged": true}'


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextmanager
def _txn(conn):
    """One write transaction. Called inside another one it joins it (so a caller can make several steps all-or-nothing)."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _subject_exists(conn, subject_id):
    if subject_id.startswith("src_"):
        return conn.execute("SELECT 1 FROM sources WHERE source_id = ?", (subject_id,)).fetchone() is not None
    if subject_id.startswith("uv_"):
        return conn.execute("SELECT 1 FROM unit_versions WHERE unit_version_id = ?", (subject_id,)).fetchone() is not None
    return False


# ------------------------------------------------------------------------------------------------ validation
def validate_diff(conn, kind, diff):
    """Shape and existence checks, done when a proposal is made and again when it is applied (the world may have moved)."""
    if kind not in KINDS:
        raise ReviewRefused(f"unknown proposal kind {kind!r}")
    if kind not in SUPPORTED:
        raise ReviewRefused(f"proposal kind {kind!r} is not available yet (purge needs a policy decision; merge_tag is not built)")
    if not isinstance(diff, dict):
        raise ReviewRefused("a proposal's difference must be a mapping")
    if kind in ("ai_edit", "metadata"):
        stmts = diff.get("statements")
        if not isinstance(stmts, list) or not stmts:
            raise ReviewRefused("a proposal needs a non-empty list of statements")
        if len(stmts) > MAX_STATEMENTS:
            raise ReviewRefused(f"a proposal may change at most {MAX_STATEMENTS} statements")
        for s in stmts:
            if not isinstance(s, dict) or not isinstance(s.get("subject_id"), str) or not _subject_exists(conn, s["subject_id"]):
                raise ReviewRefused("every statement must name an existing source or unit version")
            if commit.is_hidden(conn, s["subject_id"]):
                raise ReviewRefused("a hidden source or unit cannot be the subject of a proposal")
            if s.get("key") not in STATEMENT_KEYS:
                raise ReviewRefused(f"statement key {s.get('key')!r} is not one the review layer may write")
            if not isinstance(s.get("value"), str) or not s["value"].strip() or len(s["value"]) > MAX_VALUE:
                raise ReviewRefused(f"a statement value must be a non-empty string of at most {MAX_VALUE} characters")
            sup = s.get("supersedes")
            if sup is not None:
                row = conn.execute("SELECT subject_id, key FROM statements WHERE statement_id = ?", (sup,)).fetchone()
                if row is None or row[0] != s["subject_id"] or row[1] != s["key"]:
                    raise ReviewRefused("'supersedes' must name an existing statement with the same subject and key")
        if kind == "metadata" and diff.get("origin", "human") not in ("human", "ai"):
            raise ReviewRefused("a metadata proposal's origin is 'human' or 'ai'")
    elif kind == "delete":
        target, level = diff.get("target_id"), diff.get("level")
        if level not in LEVELS:
            raise ReviewRefused("level c (physical erasure) needs a policy decision that has not been made; only levels a and b exist"
                                if level == "c" else "deletion level must be 'a' (not searchable) or 'b' (hidden from general access)")
        found = isinstance(target, str) and (
            (target.startswith("src_") and conn.execute("SELECT 1 FROM sources WHERE source_id = ?", (target,)).fetchone()) or
            (target.startswith("unit_") and conn.execute("SELECT 1 FROM units WHERE unit_id = ?", (target,)).fetchone()))
        if not found:
            raise ReviewRefused("a deletion target must be an existing source or unit")
    elif kind == "patch":
        patches.validate_diff(conn, diff)


# ------------------------------------------------------------------------------------------------ lifecycle
def _row(conn, proposal_id):
    r = conn.execute("SELECT proposal_id, kind, diff_json, requested_by, state, base_commit_id FROM proposals WHERE proposal_id = ?",
                     (proposal_id,)).fetchone()
    if r is None:
        raise ReviewRefused(f"no proposal {proposal_id}")
    return {"proposal_id": r[0], "kind": r[1], "diff": json.loads(r[2]), "requested_by": r[3], "state": r[4], "base_commit_id": r[5]}


def create(conn, kind, diff, requested_by, now=None):
    """Make a proposal against the active commit. Nothing about the knowledge changes."""
    if not isinstance(requested_by, str) or not requested_by.strip():
        raise ReviewRefused("a proposal records who asked for it")
    validate_diff(conn, kind, diff)
    pid = ids.new_id("pr")
    with _txn(conn):
        conn.execute("INSERT INTO proposals(proposal_id, kind, diff_json, requested_by, state, base_commit_id, created_at) VALUES (?,?,?,?, 'pending', ?, ?)",
                     (pid, kind, json.dumps(diff, ensure_ascii=False, sort_keys=True), requested_by, commit.active_commit_id(conn), now or _now()))
        audit.append(conn, "proposal_created", {"proposal_id": pid})
    return pid


def _mark_stale(conn, proposal_id, now):
    with _txn(conn):
        conn.execute("UPDATE proposals SET state = 'stale', decided_at = ? WHERE proposal_id = ? AND state IN ('pending','approved')",
                     (now or _now(), proposal_id))
        audit.append(conn, "proposal_stale", {"proposal_id": proposal_id})


def approve(conn, proposal_id, by, override_protected=False, acknowledge_unsupported=False, now=None):
    """The user's decision. Refused for anything but a pending proposal; a proposal made against an older commit becomes stale and raises.
    Everything is read AFTER the write lock is taken, so nothing can move between the check and the write."""
    if not isinstance(by, str) or not by.strip():
        raise ReviewRefused("an approval records who approved")
    aid, stale = ids.new_id("ap"), False
    with _txn(conn):
        p = _row(conn, proposal_id)
        if p["state"] != "pending":
            raise ReviewRefused(f"proposal {proposal_id} is {p['state']}, not pending")
        if p["base_commit_id"] != commit.active_commit_id(conn):
            _mark_stale(conn, proposal_id, now)
            stale = True
        else:
            if override_protected and p["kind"] != "ai_edit":
                raise ReviewRefused("override_protected only applies to an AI edit that replaces human-written content")
            if p["kind"] == "ai_edit":
                _require_citations(conn, p["diff"])
                if (p["diff"].get("checks") or {}).get("unsupported") and not acknowledge_unsupported:
                    raise ReviewRefused("the support check found claims the cited evidence does not support; approve only with acknowledge_unsupported")
            conn.execute("INSERT INTO approvals(approval_id, proposal_id, approved_by, at, override_protected) VALUES (?,?,?,?,?)",
                         (aid, proposal_id, by, now or _now(), 1 if override_protected else 0))
            conn.execute("UPDATE proposals SET state = 'approved', decided_at = ? WHERE proposal_id = ?", (now or _now(), proposal_id))
            audit.append(conn, "proposal_approved", {"proposal_id": proposal_id, "approval_id": aid})
    if stale:
        raise StaleProposal(f"proposal {proposal_id} was made against an older version of the knowledge; make it again")
    return aid


def _require_citations(conn, diff):
    """Recomputed at approval: a stored 'checks' field is never trusted. Every AI edit that writes text (a summary, a note, a unit's text) must carry
    at least one claim, every claim must carry citations, and every citation must exist (the unit version exists and the quotation is in it)."""
    claims = diff.get("claims") or []
    cited = list(diff.get("citations") or []) + [c for cl in claims for c in cl.get("citations") or []]
    writes_text = any(s.get("key") in FACTUAL_KEYS for s in diff.get("statements") or [])
    uncited = [i for i, cl in enumerate(claims) if not cl.get("citations")]
    if uncited or (writes_text and not cited) or evidence.missing_citations(conn, cited):
        raise ReviewRefused("a cited source or quotation does not exist, a claim has no citation, or an edit that writes text cites nothing; "
                            "an AI edit with an invented or missing citation cannot be approved")


def reject(conn, proposal_id, by, now=None):
    p = _row(conn, proposal_id)
    if p["state"] != "pending":
        raise ReviewRefused(f"proposal {proposal_id} is {p['state']}, not pending")
    with _txn(conn):
        conn.execute("UPDATE proposals SET state = 'rejected', decided_at = ? WHERE proposal_id = ?", (now or _now(), proposal_id))
        audit.append(conn, "proposal_rejected", {"proposal_id": proposal_id})


def apply(conn, proposal_id, now=None):
    """Write an approved proposal, all of it or none of it. Returns a small summary. Visible to readers only after the next commit."""
    stale, summary = False, None
    with _txn(conn):                                  # the checks and the writes share one write lock
        p = _row(conn, proposal_id)
        if p["state"] != "approved":
            raise ReviewRefused(f"proposal {proposal_id} is {p['state']}; only an approved proposal can be applied")
        if p["base_commit_id"] != commit.active_commit_id(conn):
            _mark_stale(conn, proposal_id, now)
            stale = True
        else:
            approval = conn.execute("SELECT approved_by, override_protected FROM approvals WHERE proposal_id = ?", (proposal_id,)).fetchone()
            if approval is None:
                raise ReviewRefused("an approved proposal without an approval record cannot be applied")
            validate_diff(conn, p["kind"], p["diff"])
            if p["kind"] == "ai_edit":
                _require_citations(conn, p["diff"])                     # again: the evidence may have changed since the approval
            if p["kind"] in ("ai_edit", "metadata"):
                summary = _apply_statements(conn, p, approval)
            elif p["kind"] == "delete":
                summary = _apply_delete(conn, p, now)
            else:
                summary = patches.apply_proposal(conn, proposal_id, p["diff"], now)
            conn.execute("UPDATE proposals SET state = 'applied' WHERE proposal_id = ?", (proposal_id,))
            audit.append(conn, "proposal_applied", {"proposal_id": proposal_id})
    if stale:
        raise StaleProposal(f"the knowledge changed after proposal {proposal_id} was approved; it was marked stale and not applied")
    return summary


def _apply_statements(conn, p, approval):
    origin = "ai" if p["kind"] == "ai_edit" else p["diff"].get("origin", "human")
    override = bool(approval[1])
    written = []
    for s in p["diff"]["statements"]:
        subject, key, value = s["subject_id"], s["key"], s["value"]
        supersedes = s.get("supersedes")
        if key not in MULTI_VALUED_KEYS:
            head = commit.statement_head(conn, subject, key)
            head_origin = conn.execute("SELECT origin FROM statements WHERE statement_id = ?", (head,)).fetchone()[0] if head else None
            if origin == "ai" and head_origin == "human" and not override:
                raise ReviewRefused("this AI edit would replace human-written content; the approval must carry override_protected "
                                    "after the user has seen the difference")
            if supersedes is None:
                supersedes = head
        evidence = {"proposal_id": p["proposal_id"]}
        if origin == "ai" and p["diff"].get("citations"):
            evidence["citations"] = p["diff"]["citations"]
        sid = ids.statement_id(subject, key, value, origin, evidence)
        conn.execute("INSERT OR IGNORE INTO statements(statement_id, subject_id, key, value_json, origin, evidence_json, supersedes, state, proposal_id) "
                     "VALUES (?,?,?,?,?,?,?, 'candidate', ?)",
                     (sid, subject, key, json.dumps(value, ensure_ascii=False), origin, json.dumps(evidence, sort_keys=True), supersedes, p["proposal_id"]))
        written.append(sid)
    return {"statements": written}


def _apply_delete(conn, p, now):
    target, level = p["diff"]["target_id"], p["diff"]["level"]
    tid = ids.new_id("tomb")
    conn.execute("INSERT INTO tombstones(tombstone_id, target_id, level, proposal_id, at) VALUES (?,?,?,?,?)", (tid, target, level, p["proposal_id"], now or _now()))
    audit.append(conn, "tombstone", {"target_id": target, "level": level, "proposal_id": p["proposal_id"]})
    return {"tombstone": tid, "target_id": target, "level": level}


def resolve_patch(conn, patch_id, accept, by, now=None):
    """Decide a patch that replay could not place by itself. accept: make the same correction on the unit replay suggests, as a new proposal that
    this call approves and applies (the person calling is the one deciding). Otherwise the patch is dropped. Returns the new proposal id or None."""
    new_pid = None
    with _txn(conn):                                   # the new correction and the end of the old one happen together or not at all
        row = conn.execute("SELECT anchor_json, op, payload_json, replay_state FROM patches WHERE patch_id = ?", (patch_id,)).fetchone()
        if row is None or row[3] != "needs_review":
            raise ReviewRefused(f"patch {patch_id} is not waiting for a decision")
        if accept:
            suggest = json.loads(row[0]).get("suggest")
            if not suggest or not suggest.get("uv"):
                raise ReviewRefused("replay found no unit to suggest; the patch can only be dropped")
            new_pid = create(conn, "patch", dict(json.loads(row[2]), unit_version_id=suggest["uv"], op=row[1]), by, now)
            approve(conn, new_pid, by, now=now)
            apply(conn, new_pid, now)
        conn.execute("UPDATE patches SET replay_state = 'orphaned' WHERE patch_id = ?", (patch_id,))
    return new_pid


def request_delete(conn, target_id, level, requested_by, now=None):
    """Deletion is a proposal like any other: nothing is hidden until it is approved, applied and committed."""
    return create(conn, "delete", {"target_id": target_id, "level": level}, requested_by, now)


# ------------------------------------------------------------------------------------------------ retention
def purge_expired(conn, retention_days=RETENTION_DAYS, now=None):
    """Blank the diff of decided (rejected, applied, stale) proposals older than the retention period. Pending and approved proposals are never
    touched. Returns the number of proposals blanked."""
    now = now or _now()
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(calendar.timegm(time.strptime(now, "%Y-%m-%dT%H:%M:%SZ")) - retention_days * 86400))
    with _txn(conn):
        cur = conn.execute("UPDATE proposals SET diff_json = ? WHERE state IN ('rejected','applied','stale') AND decided_at IS NOT NULL "
                           "AND decided_at <= ? AND diff_json != ?", (PURGED, cutoff, PURGED))
        if cur.rowcount:
            audit.append(conn, "proposals_purged", {"count": cur.rowcount})
    return cur.rowcount


# ------------------------------------------------------------------------------------------------ what the user sees before deciding
def _label(conn, target_id):
    """A person decides on a name and a beginning of text, not on an identifier."""
    if target_id.startswith("src_"):
        name = conn.execute("SELECT value FROM source_locations WHERE source_id = ? AND kind = 'name' ORDER BY seq DESC LIMIT 1", (target_id,)).fetchone()
        head = commit.statement_head(conn, target_id, "title")
        title = json.loads(conn.execute("SELECT value_json FROM statements WHERE statement_id = ?", (head,)).fetchone()[0]) if head else None
        return f"source {target_id} ({name[0] if name else 'unnamed'}; title {title!r})"
    row = conn.execute("SELECT s.value_json FROM unit_versions uv JOIN statements s ON s.subject_id = uv.unit_version_id AND s.key = 'text' "
                       "WHERE uv.unit_id = ? ORDER BY uv.rowid DESC, s.rowid DESC LIMIT 1", (target_id,)).fetchone()
    return f"unit {target_id} (begins {json.loads(row[0])[:60]!r})" if row else f"unit {target_id}"


HIDDEN_NOTE = "[hidden at level b: nothing about it is shown]"


def _general_access_denied(conn, subject_id):
    """Level b is hidden from general access, also for a proposal that was made before it was hidden."""
    return commit.hidden_level(conn, subject_id) == "b"


def pending(conn):
    return [r[0] for r in conn.execute("SELECT proposal_id FROM proposals WHERE state = 'pending' ORDER BY created_at, rowid")]


def describe(conn, proposal_id):
    """Plain-text approval screen: who asked, what changes (old and new side by side) and, for an AI edit, the evidence it cites."""
    p = _row(conn, proposal_id)
    d = p["diff"]
    lines = [f"proposal {proposal_id}  kind={p['kind']}  state={p['state']}  asked by {p['requested_by']}"]
    if d.get("purged"):
        return lines + ["(the difference was removed after the retention period)"]
    if p["kind"] in ("ai_edit", "metadata"):
        for s in d["statements"]:
            if _general_access_denied(conn, s["subject_id"]):          # hidden AFTER the proposal was made: no id, no value, no new text
                lines.append(f"  {HIDDEN_NOTE}")
                continue
            old = commit.statement_head(conn, s["subject_id"], s["key"]) if s["key"] not in MULTI_VALUED_KEYS else None
            old_val = json.loads(conn.execute("SELECT value_json FROM statements WHERE statement_id = ?", (old,)).fetchone()[0]) if old else None
            lines += [f"  {s['subject_id']} / {s['key']}", f"    now : {old_val!r}", f"    new : {s['value']!r}"]
        for c in d.get("citations") or []:
            if _general_access_denied(conn, c.get("unit_version_id") or ""):
                lines.append(f"  evidence: {HIDDEN_NOTE}")
                continue
            row = conn.execute("SELECT value_json FROM statements WHERE subject_id = ? AND key = 'text' AND origin = 'native' ORDER BY rowid DESC LIMIT 1",
                               (c.get("unit_version_id"),)).fetchone()
            lines.append(f"  evidence {c.get('unit_version_id')}: quote {c.get('quote')!r}" + ("" if row else "  [NOT FOUND]"))
        checks = d.get("checks")
        if checks:
            lines.append(f"  checks: missing={checks.get('missing', [])} unsupported={checks.get('unsupported', [])}")
    elif p["kind"] == "delete":
        what = HIDDEN_NOTE if _general_access_denied(conn, d["target_id"]) else _label(conn, d["target_id"])
        lines.append(f"  hide {what} at level {d['level']}  (the original file stays; level c is not available)")
    elif p["kind"] == "patch":
        lines += [f"  {HIDDEN_NOTE}"] if _general_access_denied(conn, d["unit_version_id"]) else patches.describe(conn, d)
    return lines
