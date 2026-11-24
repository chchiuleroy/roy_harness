"""Append-only audit log (Gate 1 P5): event types and identifiers only, never original text or diffs.

The database refuses UPDATE and DELETE on audit_events (see schema.py). This module refuses anything that is not an
identifier, a digest, a small integer or a fixed token, so free text cannot leak into the log through a careless call.
"""
import json
import re
import time

from .errors import AuditError
from .ids import HEX64_RE, ID_RE

EVENT_TYPES = frozenset({
    "init", "migrate", "blob_put", "proposal_created", "proposal_approved",
    "proposal_rejected", "proposal_applied", "commit_published", "commit_abandoned", "tombstone", "convert_failed",
    "policy_refused", "doctor", "proposal_stale", "proposals_purged", "patch_replayed", "history_pruned", "profile_changed", "text_scrubbed",
    "db_compacted", "compaction_failed",
})
TOKENS = frozenset({"a", "b", "c", "copy", "ref", "ok", "partial", "failed", "prepared", "committed", "rolled_back"})
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def _check_value(key, value):
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if value < 0 or value > 10 ** 12:
            raise AuditError(f"{key}: integer out of range")
        return
    if isinstance(value, str) and (ID_RE.match(value) or HEX64_RE.match(value) or value in TOKENS):
        return
    raise AuditError(f"{key}: only identifiers, sha256 digests, small integers and fixed tokens may be logged")


def append(conn, event_type, ids=None):
    if event_type not in EVENT_TYPES:
        raise AuditError(f"unknown audit event type {event_type!r}")
    if ids is None:
        ids = {}
    if not isinstance(ids, dict):                    # `ids or {}` would have turned [] or "" into an empty event silently
        raise AuditError("audit ids must be a mapping")
    for k, v in ids.items():
        if not KEY_RE.match(k):
            raise AuditError(f"bad audit key {k!r}")
        _check_value(k, v)
    cur = conn.execute("INSERT INTO audit_events(ts,type,ids_json) VALUES (?,?,?)",
                       (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), event_type, json.dumps(ids, sort_keys=True)))
    return cur.lastrowid


def events(conn, limit=100):
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in
            conn.execute("SELECT event_id, ts, type, ids_json FROM audit_events ORDER BY event_id DESC LIMIT ?", (limit,))]
