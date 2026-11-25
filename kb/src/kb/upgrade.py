"""Converter upgrades (Gate 1 W8; Gate 2 test T-INC1): a new converter version is tried on a sample first and replaces the old output only when the
sample's outputs differ little. Otherwise nothing changes and the report says how much would.

Distance of one source: see `distance` (units that exist in only one of the two outputs, and how far the two ordered lists are apart). The second measure is the
text a source LOSES (see `text_lost`: the share of the stored text in units that the new output no longer has unchanged).

The numbers below were MEASURED (tools/s4_upgrade_thresholds.py, docs/s4_upgrade_thresholds_sample30.json) on the 882 markdown pages of the real wiki with seven changes
made on purpose, two judged harmless (same output, curly quotes straightened) and five judged to need a person (spaces tidied, which also squeezes the indentation of code
blocks; heading paths lower-cased; code blocks dropped; tables dropped; paragraphs cut at line breaks; the judgement is mine, and "spaces tidied" was moved from the first
group to the second after the second review). Findings that shaped the gate: the distance alone lets a change that DROPS content through (code blocks dropped: 100 percent
of samples of 30 passed at 0.05 / 0.5), which is why the text-loss limit exists; and a sample of 10 misses a change that touches a fifth of the pages about one time in six,
a sample of 30 almost never. With sample 30 and all three limits, over 200 sample draws (the draw is the choice of seed, so the rate is the chance that a given seed's
sample lets the change through): same output 100 percent, curly quotes 82 percent (6 pages of 882 change, one of them by 47 percent of its text), and 0 percent for each of
the five that need a person. They remain parameters, and changing them is a decision for Roy. The sample itself is fixed by the seed, so one upgrade of one corpus gives one
outcome every time; a second look with another seed is possible through the `seed` argument. The full application after the gate passes is one transaction per source, so
a failure half way leaves some sources on the new version; running the upgrade again continues with the rest (candidates are the sources not yet on the new version).
"""
import collections
import difflib
import hashlib
import json

from . import commit, pipeline
from .canonical import canonical_bytes

SAMPLE = 30                 # sources tried before switching (measured: 10 misses a change touching a fifth of the pages one time in six)
MAX_MEAN_DISTANCE = 0.05    # the sample's mean distance must not exceed this
MAX_SINGLE_DISTANCE = 0.5   # and no sampled source may change by more than this
MAX_TEXT_LOSS = 0.05        # and no sampled source may have more than this share of its text (non-whitespace characters) dropped or rewritten


def _unit_keys(units):
    """Ordered, with repeats: a reordered or duplicated unit is a difference."""
    return [hashlib.sha256(canonical_bytes([u.kind, u.text, list(u.heading_path)])).hexdigest() for u in units]


def _current_pairs(conn, conv_id):
    """[(unit key, non-whitespace characters of its text)] of the stored output of one conversion, in document order."""
    out = []
    for kind, text_json, loc in conn.execute(
            "SELECT uv.kind, (SELECT value_json FROM statements s WHERE s.subject_id = uv.unit_version_id AND s.key='text' AND s.origin='native' "
            "ORDER BY s.rowid LIMIT 1), cu.locator_json FROM conversion_units cu JOIN unit_versions uv ON uv.unit_version_id = cu.unit_version_id "
            "WHERE cu.conv_id = ? ORDER BY cu.ordinal", (conv_id,)):
        text = json.loads(text_json)
        out.append((hashlib.sha256(canonical_bytes([kind, text, json.loads(loc).get("heading", [])])).hexdigest(), _chars(text)))
    return out


def _current_units(conn, conv_id):
    return [key for key, _ in _current_pairs(conn, conv_id)]


def _chars(text):
    return len("".join(str(text).split()))


def text_lost(old_pairs, new_units):
    """The share of the stored text (non-whitespace characters) that sits in units the new output no longer has unchanged: dropped, cut, rewritten or
    replaced by something else all count, however long the replacement is (second review, 2026-11-20: counting only the net number of characters let a
    long unit be swapped for an unrelated one of the same length). Units are matched by their key, repeats one for one. 0 when there was no text."""
    total = sum(chars for _, chars in old_pairs)
    if not total:
        return 0.0
    left = collections.Counter(_unit_keys(new_units))
    lost = 0
    for key, chars in old_pairs:
        if left[key] > 0:
            left[key] -= 1
        else:
            lost += chars
    return lost / total


def distance(old, new):
    """0 for identical outputs, up to 1. The larger of two measures: how many distinct units exist in only one output (content), and how far the
    two ordered lists are from each other (order and repeats: a reordered document, or one that lost a duplicated paragraph, is not identical)."""
    if not old and not new:
        return 0.0
    union, both = set(old) | set(new), set(old) & set(new)
    return max((len(union) - len(both)) / len(union), 1.0 - difflib.SequenceMatcher(None, old, new, autojunk=False).ratio())


def _candidates(conn, new_converter, held_back=None):
    """[(source_id, revision_id, blob_sha, name, conv_id)] of sources whose current conversion came from a converter of the same name
    (any other version). `held_back`, when given, collects the source ids that are left out because a unit of theirs is hidden."""
    out = []
    for (sid,) in conn.execute("SELECT source_id FROM sources ORDER BY source_id").fetchall():
        if commit.is_hidden(conn, sid):
            continue                      # converting a hidden source again would write its text back into the database (second review, 2026-11-20)
        cur = commit.current_conversion(conn, sid)
        if cur and cur[1] == new_converter.name and cur[2] != new_converter.version:
            if commit.revision_holds_hidden_unit(conn, cur[4]):
                if held_back is not None:
                    held_back.append(sid)
                continue                  # a unit keeps its id only while its structural key is unchanged; a new converter may cut the text differently, and the
                                          # hidden text would come back under a new id that no tombstone names (sixth review, 2026-11-23)
            sha = conn.execute("SELECT blob_sha256 FROM revisions WHERE revision_id = ?", (cur[4],)).fetchone()[0]
            out.append((sid, cur[4], sha, pipeline._latest_name(conn, sid), cur[0]))
    return out


def dual_run(conn, store, new_converter, sample=SAMPLE, seed="kb"):
    """Convert a deterministic sample with the new converter IN MEMORY and compare with what is stored. Writes nothing."""
    held = []
    cands = sorted(_candidates(conn, new_converter, held), key=lambda c: hashlib.sha256((seed + c[0]).encode()).hexdigest())
    rows, unreadable = [], 0
    for sid, _rev, sha, name, conv_id in cands[:sample]:
        data = pipeline._read_blob(conn, store, sha)
        if data is None:
            unreadable += 1
            continue
        res = new_converter.convert(data, {"filename": name})
        if res.status == "failed":
            rows.append({"source_id": sid, "distance": 1.0, "text_lost": 1.0, "new_status": "failed"})
            continue
        stored = _current_pairs(conn, conv_id)
        rows.append({"source_id": sid, "distance": distance([key for key, _ in stored], _unit_keys(res.units)),
                     "text_lost": text_lost(stored, res.units), "new_status": res.status})
    d = [r["distance"] for r in rows]
    return {"candidates": len(cands), "held_back_hidden_unit": len(held), "sampled": len(rows), "unreadable": unreadable,
            "mean_distance": sum(d) / len(d) if d else None, "max_distance": max(d) if d else None,
            "max_text_lost": max((r["text_lost"] for r in rows), default=None),
            "identical": sum(1 for x in d if x == 0.0), "rows": rows}


def switch_allowed(report, sample=SAMPLE, max_mean=MAX_MEAN_DISTANCE, max_single=MAX_SINGLE_DISTANCE, max_loss=MAX_TEXT_LOSS):
    """(allowed, reason). Not allowed when there is nothing to compare, the sample is smaller than it should be, or the outputs differ too much."""
    if not report["sampled"]:
        return False, "no source could be compared"
    if report["sampled"] < min(sample, report["candidates"]):
        return False, "the sample is smaller than it should be (some originals could not be read)"
    if report["max_distance"] > max_single:
        return False, f"one sampled source changes by {report['max_distance']:.0%}"
    if report["max_text_lost"] > max_loss:
        return False, f"one sampled source loses {report['max_text_lost']:.0%} of its text (limit {max_loss:.0%})"
    if report["mean_distance"] > max_mean:
        return False, f"the sample changes by {report['mean_distance']:.1%} on average (limit {max_mean:.0%})"
    return True, "the sample's outputs are close to the stored ones"


def upgrade(conn, store, new_converter, sample=SAMPLE, seed="kb"):
    """Dual-run, then re-convert every source of that converter with the new version if (and only if) the gate allows it.
    Returns (report, allowed, reason, converted_revisions)."""
    report = dual_run(conn, store, new_converter, sample, seed)
    allowed, reason = switch_allowed(report, sample)
    done = 0
    if allowed:
        for _sid, rev, _sha, _name, _conv in _candidates(conn, new_converter):
            conn.execute("BEGIN IMMEDIATE")
            try:
                pipeline.process_revision(conn, store, rev, converter=new_converter)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            done += 1
    return report, allowed, reason, done
