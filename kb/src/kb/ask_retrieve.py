"""Retrieval for a question, before any model sees anything (stage S4).

Deterministic code on top of the same `Searcher` the command line uses, so the committed-only rule, the hidden-source rule and the honest status are
the ones already tested. Added here: only collections that are visible to agents may reach a model (and, when the endpoint is not on this machine,
only collections that allow an external model); each passage is labelled with how its source entered kb; each passage is screened (ask_screen);
a flagged passage is left out of the prompt and reported to the person. What was left out is reported by COUNT and reason, never by id, so the
report cannot be used to find out whether a hidden item exists.
"""
from dataclasses import dataclass, field
from typing import List

from . import ask_screen, colls
from .errors import KbError, PolicyViolation
from . import profile as profilemod

ORIGINS = {"path": "file", "url": "url", "paste": "paste"}
OUTSIDE = ("url", "paste", "other")   # origins that are NOT known to be the person's own files (an unknown one counts as outside): the answer waits for a person (defence step 4)


@dataclass
class Passage:
    pid: str
    chunk_id: str
    source_id: str
    name: str
    page: object
    origin: str
    text: str
    status: str = "ok"
    locator: dict = field(default_factory=dict)


@dataclass
class Retrieval:
    commit_id: str
    passages: List[Passage]
    flagged: list                    # [{"chunk_id", "source_id", "name", "page", "origin", "reasons"}]: left out of the prompt
    excluded: dict                   # reason -> count


def origin_of(conn, source_id):
    row = conn.execute("SELECT kind FROM source_locations WHERE source_id = ? AND kind IN ('path', 'url', 'paste') ORDER BY seq DESC LIMIT 1",
                       (source_id,)).fetchone()
    return ORIGINS.get(row[0], "other") if row else "other"


def _profile_of_source(conn, source_id):
    row = conn.execute("SELECT collection_id FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    return colls.get_profile(conn, row[0]) if row else None


def retrieve(conn, searcher, commit_id, question, k=6, mode="auto", external_endpoint=True, oversample=4):
    """The top `k` passages that may be sent to the model. `external_endpoint`: the model is not on this machine."""
    hits = searcher.chunks(question, max(k * oversample, k), mode)
    excluded = {}
    passages, flagged = [], []
    for hit in hits:
        if hit.state != "committed" or not hit.chunk_id:
            continue
        prof = _profile_of_source(conn, hit.source_id)
        try:
            if prof is None:
                raise PolicyViolation("unknown collection")
            profilemod.require_agent_visible(prof)
            if external_endpoint:
                profilemod.require_external_model(prof)
        except PolicyViolation as e:
            reason = "the collection is not visible to agents" if "agents" in str(e) else "the collection must not be sent to an external model"
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        row = conn.execute("SELECT text FROM chunks WHERE chunk_id = ?", (hit.chunk_id,)).fetchone()
        if row is None:
            raise KbError("a hit has no chunk text; the index and the database disagree")
        origin = origin_of(conn, hit.source_id)
        page = hit.locator.get("page")
        reasons = ask_screen.screen(row[0])
        if reasons:
            flagged.append({"chunk_id": hit.chunk_id, "source_id": hit.source_id, "name": hit.name, "page": page, "origin": origin, "reasons": reasons})
            continue
        if len(passages) < k:
            passages.append(Passage(f"P{len(passages) + 1}", hit.chunk_id, hit.source_id, hit.name, page, origin, row[0], hit.status, dict(hit.locator)))
    return Retrieval(commit_id, passages, flagged, excluded)
