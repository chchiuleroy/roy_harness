"""Human corrections as patches (Gate 1 W6, P3; Gate 2 section 4.9, tests T-P1..T-P4).

A correction never edits the original evidence or the converter's output. It is a new statement on a unit version (`text`, origin human) plus a
patch record that remembers WHERE it was made (an anchor) and WHAT it did (an operation), so it can be replayed when the converter's output
changes. A wrong application is worse than a missing one, so replay only acts alone when the evidence is exact:

    present   the very unit version is still in the current conversion          nothing to do (its statement is still attached)
    moved     one unit with identical text exists AND has the same neighbour       applied automatically, anchor follows the unit
              before or after as the old place (the unit id changed because
              paragraphs before it moved), or several identical units exist but
              only one has the same text on both sides
    relocated the only identical unit shares no neighbour with the old place       needs_review (it may be unrelated boilerplate;
              (a shared heading alone is not enough)                               a document edge, None, is not a neighbour)
    changed   the same unit now has other text                                    needs_review (a person decides)
    similar   no such unit, but a unit with similar text and the same heading     needs_review
    ambiguous several identical units and no way to tell which                    needs_review
    orphaned  nothing similar is left                                             orphaned (kept, never applied)

Several corrections of one unit apply in the order they were made, each on the result of the one before.
"""
import difflib
import json
import time
from dataclasses import dataclass
from typing import Optional

from . import audit, commit, ids
from .errors import ReviewRefused

OPS = ("set_text", "replace")
SIMILAR_MIN = 0.6               # similarity at or above which a changed or relocated unit is offered to the user as the likely target
MAX_BASE = 20000


@dataclass(frozen=True)
class Resolution:
    kind: str                   # present | moved | relocated | changed | similar | ambiguous | orphaned
    uv: Optional[str] = None
    score: float = 0.0


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def native_text(conn, unit_version_id):
    row = conn.execute("SELECT value_json FROM statements WHERE subject_id = ? AND key = 'text' AND origin = 'native' ORDER BY rowid LIMIT 1",
                       (unit_version_id,)).fetchone()
    return json.loads(row[0]) if row else None


def shown_text(conn, unit_version_id):
    head = commit.statement_head(conn, unit_version_id, "text")
    if head is None:
        return native_text(conn, unit_version_id)
    return json.loads(conn.execute("SELECT value_json FROM statements WHERE statement_id = ?", (head,)).fetchone()[0])


def apply_op(text, op, params):
    if op == "set_text":
        return params["new"]
    if op == "replace":
        if text.count(params["find"]) != 1:
            raise ReviewRefused("the text to replace must occur exactly once in the unit")
        return text.replace(params["find"], params["replace"], 1)
    raise ReviewRefused(f"unknown patch operation {op!r}")


def _params(diff):
    return {"new": diff.get("new")} if diff.get("op") == "set_text" else {"find": diff.get("find"), "replace": diff.get("replace")}


def validate_diff(conn, diff):
    uv = diff.get("unit_version_id")
    if not isinstance(uv, str) or native_text(conn, uv) is None:
        raise ReviewRefused("a patch must name an existing unit version")
    if commit.is_hidden(conn, uv):
        raise ReviewRefused("a hidden unit cannot be patched")
    op = diff.get("op")
    if op not in OPS:
        raise ReviewRefused(f"patch operation must be one of {OPS}")
    p = _params(diff)
    if op == "set_text" and (not isinstance(p["new"], str) or not p["new"].strip()):
        raise ReviewRefused("set_text needs the new text")
    if op == "replace" and (not isinstance(p["find"], str) or not p["find"] or not isinstance(p["replace"], str)):
        raise ReviewRefused("replace needs the text to find and its replacement")
    current = shown_text(conn, uv)
    if apply_op(current, op, p) == current:
        raise ReviewRefused("this patch changes nothing")


# ------------------------------------------------------------------------------------------------ anchors
def unit_list(conn, conv_id):
    """The units of one conversion in document order: what an anchor is resolved against."""
    out = []
    for ordinal, uv, unit_id, fp, loc in conn.execute(
            "SELECT cu.ordinal, cu.unit_version_id, uv.unit_id, uv.fingerprint, cu.locator_json FROM conversion_units cu "
            "JOIN unit_versions uv ON uv.unit_version_id = cu.unit_version_id WHERE cu.conv_id = ? ORDER BY cu.ordinal", (conv_id,)):
        loc = json.loads(loc)
        out.append({"ordinal": ordinal, "uv": uv, "unit_id": unit_id, "fingerprint": fp, "heading": loc.get("heading", []),
                    "key": loc.get("key"), "text": native_text(conn, uv) or ""})
    for i, u in enumerate(out):                      # the neighbours' fingerprints: what tells two identical units apart
        u["before"] = out[i - 1]["fingerprint"] if i > 0 else None
        u["after"] = out[i + 1]["fingerprint"] if i + 1 < len(out) else None
    return out


def make_anchor(conn, unit_version_id):
    unit_id, conv_id, fp = conn.execute("SELECT unit_id, conv_id, fingerprint FROM unit_versions WHERE unit_version_id = ?", (unit_version_id,)).fetchone()
    units = unit_list(conn, conv_id)
    u = next(u for u in units if u["uv"] == unit_version_id)
    return {"uv": unit_version_id, "unit_id": unit_id, "source_id": commit.source_of(conn, unit_version_id), "fingerprint": fp,
            "key": u["key"], "heading": u["heading"], "base_text": u["text"][:MAX_BASE], "before": u["before"], "after": u["after"]}


def resolve(anchor, units):
    """Pure function: where does the anchored unit stand in this list of units? (T-P1..T-P3)"""
    for u in units:
        if u["uv"] == anchor["uv"]:
            return Resolution("present", u["uv"], 1.0)
    same = [u for u in units if u["fingerprint"] == anchor["fingerprint"]]
    if len(same) == 1:                               # the only identical unit: a shared NEIGHBOUR is needed; a shared heading alone could be boilerplate
        u = same[0]
        neighbour = ((u["before"] is not None and u["before"] == anchor["before"]) or
                     (u["after"] is not None and u["after"] == anchor["after"]))        # two document edges (None) are not evidence of anything
        return Resolution("moved" if neighbour else "relocated", u["uv"], 1.0)
    if len(same) > 1:                                # identical units: only identical neighbours on both sides identify the one that was patched
        placed = [u for u in same if u["before"] == anchor["before"] and u["after"] == anchor["after"]]
        return Resolution("moved", placed[0]["uv"], 1.0) if len(placed) == 1 else Resolution("ambiguous", None, 1.0)
    base = anchor.get("base_text", "")

    def ratio(u):
        return difflib.SequenceMatcher(None, base, u["text"], autojunk=False).ratio()
    same_unit = [u for u in units if u["unit_id"] == anchor["unit_id"]]
    if same_unit:
        return Resolution("changed", same_unit[0]["uv"], round(ratio(same_unit[0]), 3))
    near = [(ratio(u), u) for u in units if u["heading"] == anchor["heading"]]
    best = max(near, key=lambda t: t[0], default=None)
    if best and best[0] >= SIMILAR_MIN:
        return Resolution("similar", best[1]["uv"], round(best[0], 3))
    return Resolution("orphaned", None, 0.0)


# ------------------------------------------------------------------------------------------------ writing and replaying
def _write_text_statement(conn, uv, text, proposal_id, patch_id, replay_of=None):
    evidence = {"proposal_id": proposal_id, "patch_id": patch_id}
    if replay_of:
        evidence["replay_of"] = replay_of
    sid = ids.statement_id(uv, "text", text, "human", evidence)
    conn.execute("INSERT OR IGNORE INTO statements(statement_id, subject_id, key, value_json, origin, evidence_json, supersedes, state, proposal_id) "
                 "VALUES (?,?,?,?, 'human', ?, ?, 'candidate', ?)",
                 (sid, uv, "text", json.dumps(text, ensure_ascii=False), json.dumps(evidence, sort_keys=True), commit.statement_head(conn, uv, "text"), proposal_id))
    return sid


def apply_proposal(conn, proposal_id, diff, now=None):
    """Called by review.apply inside its transaction: record the patch and write the corrected text as a human statement."""
    uv = diff["unit_version_id"]
    params = _params(diff)
    text = apply_op(shown_text(conn, uv), diff["op"], params)
    anchor = make_anchor(conn, uv)
    patch_id = "pat_" + proposal_id[3:]
    conn.execute("INSERT INTO patches(patch_id, unit_id, anchor_json, op, payload_json, replay_state, created_at, proposal_id) VALUES (?,?,?,?,?, 'applied', ?, ?)",
                 (patch_id, anchor["unit_id"], json.dumps(anchor, ensure_ascii=False, sort_keys=True), diff["op"],
                  json.dumps(params, ensure_ascii=False, sort_keys=True), now or _now(), proposal_id))
    return {"patch_id": patch_id, "statement": _write_text_statement(conn, uv, text, proposal_id, patch_id)}


def _rows(conn, states):
    marks = ",".join("?" * len(states))
    return [{"patch_id": r[0], "anchor": json.loads(r[1]), "op": r[2], "params": json.loads(r[3]), "state": r[4], "proposal_id": r[5]}
            for r in conn.execute(f"SELECT patch_id, anchor_json, op, payload_json, replay_state, proposal_id FROM patches "
                                  f"WHERE replay_state IN ({marks}) ORDER BY created_at, rowid", states)]


def _set(conn, patch_id, state, anchor=None):
    if anchor is None:
        conn.execute("UPDATE patches SET replay_state = ? WHERE patch_id = ?", (state, patch_id))
    else:
        conn.execute("UPDATE patches SET replay_state = ?, anchor_json = ? WHERE patch_id = ?",
                     (state, json.dumps(anchor, ensure_ascii=False, sort_keys=True), patch_id))


def replay(conn):
    """Re-place every live patch against the current conversion of its source (the caller holds the writer lock and a transaction is NOT open).
    Returns counts per outcome."""
    counts = {"present": 0, "moved": 0, "needs_review": 0, "orphaned": 0}
    groups = {}
    for r in _rows(conn, ("applied", "needs_review")):
        groups.setdefault(r["anchor"]["uv"], []).append(r)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for uv, rows in groups.items():
            anchor = rows[0]["anchor"]
            source = anchor["source_id"]
            if commit.tombstone_level(conn, source):
                continue
            conv = commit.current_conversion(conn, source)
            if conv is None:
                continue
            res = resolve(anchor, unit_list(conn, conv[0]))
            if res.kind == "present":
                for r in rows:
                    _set(conn, r["patch_id"], "applied")
                counts["present"] += 1
            elif res.kind == "moved":
                try:
                    text = native_text(conn, res.uv)
                    for r in rows:
                        text = apply_op(text, r["op"], r["params"])
                except ReviewRefused:                          # the unit moved but a find-and-replace no longer fits: a person decides
                    res = Resolution("changed", res.uv, 1.0)
                else:
                    last = rows[-1]
                    _write_text_statement(conn, res.uv, text, last["proposal_id"], last["patch_id"], replay_of=uv)
                    for r in rows:
                        _set(conn, r["patch_id"], "applied", dict(r["anchor"], uv=res.uv))
                        audit.append(conn, "patch_replayed", {"patch_id": r["patch_id"]})
                    counts["moved"] += 1
                    continue
            if res.kind in ("changed", "similar", "ambiguous", "relocated"):
                for r in rows:
                    _set(conn, r["patch_id"], "needs_review", dict(r["anchor"], suggest={"kind": res.kind, "uv": res.uv, "score": res.score}))
                counts["needs_review"] += 1
            elif res.kind == "orphaned":
                for r in rows:
                    _set(conn, r["patch_id"], "orphaned")
                counts["orphaned"] += 1
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return counts


def queue(conn):
    """The conflict queue: patches that need a person, each with what replay thinks they now point at."""
    return [{"patch_id": r["patch_id"], "suggest": r["anchor"].get("suggest"), "op": r["op"]} for r in _rows(conn, ("needs_review",))]


def describe(conn, diff):
    uv = diff["unit_version_id"]
    current = shown_text(conn, uv)
    return [f"  unit version {uv}  operation {diff['op']}", f"    now : {current!r}", f"    new : {apply_op(current, diff['op'], _params(diff))!r}"]
