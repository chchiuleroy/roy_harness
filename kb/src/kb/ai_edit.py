"""AI correction flow (Gate 1 W7; Gate 2 test T-AI1): the user asks, a proposer produces a difference, the user reviews and approves, only then
does anything change. In S2 the proposer is any function (a scripted one in tests); no model is called.

What this module guarantees, and where the rest is guaranteed:
  * it only ever CREATES a proposal (review.create); the knowledge is unchanged until review.approve and review.apply;
  * the proposer's output is untrusted data: review validates it (existing subjects, allowed keys, sizes); it can never set the origin;
  * a proposer that is an external model gets text only from a collection whose profile allows external models (profile.require_external_model);
  * cited evidence is checked for EXISTENCE here (stored for the approval screen) and again, independently, by review.approve; SUPPORT is a judge's
    opinion shown to the user, never a gate on its own (evidence.py).
"""
import json

from . import colls, commit, evidence, patches, profile, review
from .errors import ReviewRefused

MAX_STATEMENTS = 50


def _collection_of(conn, subject_id):
    src = commit.source_of(conn, subject_id)
    row = conn.execute("SELECT collection_id FROM sources WHERE source_id = ?", (src,)).fetchone() if src else None
    return row[0] if row else None


def context_for(conn, target_id):
    """What the proposer is shown: the current displayed values of the target and nothing else."""
    if target_id.startswith("uv_"):
        return {"target_id": target_id, "text": patches.shown_text(conn, target_id)}
    head = commit.statement_head(conn, target_id, "title")
    title = conn.execute("SELECT value_json FROM statements WHERE statement_id = ?", (head,)).fetchone() if head else None
    return {"target_id": target_id, "title": json.loads(title[0]) if title else None}


def propose(conn, proposer, target_id, instruction, requested_by, judge=evidence.overlap_judge, external=False, now=None):
    """Returns the id of a pending ai_edit proposal. Raises ReviewRefused for anything that must not even become a proposal."""
    cid = _collection_of(conn, target_id)
    if cid is None:
        raise ReviewRefused("the target must be an existing source or unit version")
    if commit.is_hidden(conn, target_id):
        raise ReviewRefused("a hidden source or unit is not shown to a proposer")
    if external:
        profile.require_external_model(colls.get_profile(conn, cid))          # raises PolicyViolation: the text is not shown to an external model
    out = proposer(dict(context_for(conn, target_id), instruction=instruction))
    if not isinstance(out, dict) or not isinstance(out.get("statements"), list):
        raise ReviewRefused("the proposer did not return a list of statements")
    if len(out["statements"]) > MAX_STATEMENTS:
        raise ReviewRefused(f"a proposal may change at most {MAX_STATEMENTS} statements")
    claims = [c for c in out.get("claims") or [] if isinstance(c, dict)]
    diff = {"statements": out["statements"], "claims": claims, "instruction": instruction,
            "citations": [c for cl in claims for c in cl.get("citations") or []] + list(out.get("citations") or [])}
    checks = evidence.check_claims(conn, claims, judge)
    checks["missing"] = [[i, why] for i, why in checks["missing"]]
    diff["checks"] = checks
    return review.create(conn, "ai_edit", diff, requested_by, now)
