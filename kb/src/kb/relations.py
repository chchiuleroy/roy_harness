"""Deterministic relations between sources: the first, cheapest step towards a knowledge graph (decided with Roy on 2026-11-16: relations that can be
read off the data without a language model come first; anything a model would extract is a candidate that needs approval, and is not built here).

A markdown wikilink [[Name]] is stored by the pipeline as a native statement 'links_to' (the text as written). derive() resolves each one to a
source: the link text must match, case-insensitively, exactly one source's file-name stem (spaces read as underscores) or displayed title, and
must not point at the source itself. Each resolved link becomes a DERIVED statement 'links_to_source' whose value is {"ref": <source id>} with
the rule and the link text as evidence. Ambiguous and unresolved links create nothing and are counted. Derived edges that no longer resolve
(target renamed, link edited out) are marked rejected, never deleted, so history stays and commits stop including them.
"""
import json

from . import commit, ids

RULE = "wikilink_name_match@1"


def _stem(name):
    return name.rsplit(".", 1)[0] if "." in name else name


def _norm(text):
    return text.strip().casefold().replace(" ", "_")


def derive(conn):
    """Recompute edges from the current conversions. Returns counts: resolved, ambiguous, unresolved, rejected, restored."""
    by_name, by_title, current = {}, {}, {}
    visible = {sid for sid, _ in commit.included_sources(conn)}          # hidden or removed sources are neither ends of an edge
    for (source_id,) in conn.execute("SELECT source_id FROM sources"):
        if source_id not in visible:
            continue
        conv = commit.current_conversion(conn, source_id)
        if conv is None:
            continue
        current[source_id] = conv[0]
        row = conn.execute("SELECT value FROM source_locations WHERE source_id=? AND kind='name' ORDER BY seq DESC LIMIT 1", (source_id,)).fetchone()
        if row:
            by_name.setdefault(_norm(_stem(row[0])), set()).add(source_id)
        shown = commit._subject_statements(conn, source_id, conv[0], {}, only_key="title")
        if shown.get("title"):
            by_title.setdefault(_norm(shown["title"]), set()).add(source_id)
    desired, counts = {}, {"resolved": 0, "ambiguous": 0, "unresolved": 0, "rejected": 0, "restored": 0}
    for sid, subject, value_json, evidence in conn.execute("SELECT statement_id, subject_id, value_json, evidence_json FROM statements "
                                                            "WHERE key='links_to' AND state != 'rejected'").fetchall():
        if subject not in current or json.loads(evidence).get("conv_id") != current[subject]:
            continue
        link = json.loads(value_json)
        found = (by_name.get(_norm(link), set()) | by_title.get(_norm(link), set())) - {subject}
        if len(found) == 1:
            target = next(iter(found))
            edge = ids.statement_id(subject, "links_to_source", {"ref": target}, "derived", {"rule": RULE, "link": link})
            desired[edge] = (subject, target, link)
            counts["resolved"] += 1
        elif found:
            counts["ambiguous"] += 1
        else:
            counts["unresolved"] += 1
    existing = {sid: state for sid, state in conn.execute("SELECT statement_id, state FROM statements WHERE key='links_to_source' AND origin='derived'")}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for edge, (subject, target, link) in desired.items():
            if edge not in existing:
                conn.execute("INSERT INTO statements(statement_id, subject_id, key, value_json, origin, evidence_json, state) "
                             "VALUES (?,?,?,?,?,?, 'candidate')",
                             (edge, subject, "links_to_source", json.dumps({"ref": target}), "derived",
                              json.dumps({"rule": RULE, "link": link}, sort_keys=True)))
            elif existing[edge] == "rejected":
                conn.execute("UPDATE statements SET state='candidate' WHERE statement_id=?", (edge,))
                counts["restored"] += 1
        for edge, state in existing.items():
            if edge not in desired and state != "rejected":
                conn.execute("UPDATE statements SET state='rejected' WHERE statement_id=?", (edge,))
                counts["rejected"] += 1
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return counts


def neighbors(conn, source_id, committed_only=True):
    """(outgoing, incoming) lists of source ids linked from / to this source, from the active commit (or from everything current)."""
    if committed_only:
        base = ("SELECT s.subject_id, s.value_json FROM statements s JOIN commit_statements cs ON cs.statement_id = s.statement_id "
                "WHERE cs.commit_id = (SELECT commit_id FROM active_commit WHERE id = 1) AND s.key = 'links_to_source'")
    else:
        base = "SELECT s.subject_id, s.value_json FROM statements s WHERE s.key = 'links_to_source' AND s.state != 'rejected'"
    out, inc = [], []
    visible = {sid for sid, _ in commit.included_sources(conn)}          # an edge to a source hidden since the commit is not shown
    for subject, value_json in conn.execute(base):
        target = json.loads(value_json)["ref"]
        if subject not in visible or target not in visible:
            continue
        if subject == source_id and target not in out:
            out.append(target)
        if target == source_id and subject not in inc:
            inc.append(subject)
    return sorted(out), sorted(inc)
