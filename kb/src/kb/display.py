"""Which statement is shown for a (subject, key) when several exist (Gate 2 section 4.2, 'display rule'). Applied when a commit is assembled and
then frozen in commit_statements.role, so reading an old commit never changes because newer statements appeared.

Rule: only candidate or committed statements count (rejected never). Follow `supersedes` chains: a statement that some counted statement
supersedes is history ('alternate'). Among the chain heads the order is: approved human > approved AI > native > derived; ties break on the
statement id so the choice is deterministic. Human and AI statements exist for display only with an approval. An AI statement may not take the
place of a human one unless its approval carried override_protected (the user saw the difference and agreed): otherwise it is ignored.
"""

RANK = {"human": 0, "ai": 1, "native": 2, "derived": 3}


def choose(statements, approved=frozenset(), override_ok=frozenset()):
    """statements: list of dicts with statement_id, origin, state, supersedes. Returns (display_id or None, alternates: list of ids)."""
    counted = [s for s in statements if s["state"] in ("candidate", "committed")]
    by_id = {s["statement_id"]: s for s in counted}

    def usable(s):
        if s["origin"] in ("human", "ai") and s["statement_id"] not in approved:
            return False
        return True

    def protected_conflict(s):
        # an AI statement that supersedes (directly) a human statement needs the override flag
        if s["origin"] != "ai":
            return False
        target = by_id.get(s["supersedes"])
        return target is not None and target["origin"] == "human" and s["statement_id"] not in override_ok

    eligible = {sid: s for sid, s in by_id.items() if usable(s) and not protected_conflict(s)}
    superseded = {s["supersedes"] for s in eligible.values() if s["supersedes"]}
    heads = [s for sid, s in eligible.items() if sid not in superseded]
    if not heads:
        return None, sorted(by_id)
    heads.sort(key=lambda s: (RANK[s["origin"]], s["statement_id"]))
    shown = heads[0]["statement_id"]
    return shown, sorted(sid for sid in by_id if sid != shown)
