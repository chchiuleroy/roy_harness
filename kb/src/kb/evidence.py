"""Citation checks for AI proposals (Gate 1 W7; Gate 2 test T-AI1).

Two different questions, kept apart on purpose:
  * EXISTENCE is deterministic and a hard rule: the cited unit version exists, and the quoted words really occur in its text. An invented
    citation can never be approved.
  * SUPPORT (does the quoted text back the claim?) is a judgement. It is delegated to a judge function; the baseline judge here is a plain word-overlap
    rule and its only job is to flag claims for the person who decides. `evaluate_judge` measures any judge on labelled samples, so a better judge
    can replace the baseline with numbers on the table, not by assertion. Literal matching of the claim is NOT the criterion: a faithful
    paraphrase or a synthesis of several sources must count as supported.
"""
import json
import re
import unicodedata

from . import tokenize as tk

LABELS = ("supports", "not_supported", "paraphrase_supports", "multi_source_supports")
SUPPORTED_LABELS = frozenset({"supports", "paraphrase_supports", "multi_source_supports"})


def normalise(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def native_texts(conn, unit_version_id):
    return [r[0] for r in conn.execute("SELECT value_json FROM statements WHERE subject_id = ? AND key = 'text' AND origin = 'native'", (unit_version_id,))]


def citation_problem(conn, citation):
    """None if the citation exists, else a short reason. Looks at the unit version's own converted text, never at corrected or AI text."""
    if not isinstance(citation, dict) or not isinstance(citation.get("unit_version_id"), str):
        return "malformed citation"
    quote = citation.get("quote")
    if not isinstance(quote, str) or not normalise(quote):
        return "a citation needs a quotation"
    texts = [json.loads(t) for t in native_texts(conn, citation["unit_version_id"])]
    if not texts:
        return "the cited unit version does not exist"
    q = normalise(quote)
    if not any(q in normalise(t) for t in texts):
        return "the quotation does not occur in the cited unit version"
    return None


def missing_citations(conn, citations):
    """[(index, reason)] for every citation that does not exist; empty means all cited evidence is real."""
    return [(i, p) for i, c in enumerate(citations or []) if (p := citation_problem(conn, c))]


def overlap_judge(claim, evidence_texts, threshold=0.6):
    """Baseline judge: the share of the claim's words (tokenised like the index) that appear in the union of the evidence. Deterministic and
    deliberately simple; it flags, a person decides."""
    words = set(tk.tokens(claim))
    if not words:
        return False
    have = set()
    for t in evidence_texts:
        have |= set(tk.tokens(t))
    return len(words & have) / len(words) >= threshold


def check_claims(conn, claims, judge=overlap_judge):
    """claims: [{'text', 'citations': [{'unit_version_id', 'quote'}]}]. Returns {'missing': [...], 'unsupported': [claim indexes]}."""
    missing, unsupported = [], []
    for i, claim in enumerate(claims or []):
        cites = claim.get("citations") or []
        if not cites:
            missing.append((i, "a factual claim has no citation"))
            continue
        problems = missing_citations(conn, cites)
        if problems:
            missing += [(i, p) for _, p in problems]
            continue
        if not judge(claim["text"], [c["quote"] for c in cites]):
            unsupported.append(i)
    return {"missing": missing, "unsupported": unsupported}


def evaluate_judge(judge, samples):
    """samples: [{'label', 'claim', 'evidence': [str]}] with label in LABELS. Returns accuracy per label and overall, with the two error types that
    matter apart: a claim wrongly accepted (unsafe) and a faithful claim wrongly flagged (annoying)."""
    per = {lab: [0, 0] for lab in LABELS}
    wrongly_accepted = wrongly_flagged = 0
    for s in samples:
        want = s["label"] in SUPPORTED_LABELS
        got = bool(judge(s["claim"], s["evidence"]))
        per[s["label"]][1] += 1
        per[s["label"]][0] += got == want
        wrongly_accepted += got and not want
        wrongly_flagged += want and not got
    n = sum(v[1] for v in per.values())
    return {"n": n, "accuracy": sum(v[0] for v in per.values()) / n if n else None,
            "per_label": {k: {"correct": v[0], "n": v[1]} for k, v in per.items()},
            "wrongly_accepted": wrongly_accepted, "wrongly_flagged": wrongly_flagged}
