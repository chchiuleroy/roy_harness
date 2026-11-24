"""Chunking (Gate 1 W5, Gate 2 section 4.8): strategy `heading_window` v1.

Units are grouped by section (same heading path); consecutive units of a section are joined with a blank line until the next unit would push the
chunk past `limit` characters; a single unit longer than `hard` is cut into windows at the last sentence or line boundary inside the window.
A chunk never spans two sections and never spans two sources (the caller chunks one source at a time). Every non-blank unit text lands in
exactly one chunk (blank units and blank cut-off tails are dropped: they carry nothing to index). The strategy name, version and parameter hash are part of every chunk id, so changing a parameter produces different ids and
a rebuilt index, never a quiet mix of old and new chunks.
"""
import hashlib
from dataclasses import dataclass
from typing import List, Tuple

from . import ids
from .canonical import canonical_bytes

NAME, VERSION = "heading_window", "1"
DEFAULT_PARAMS = {"limit": 800, "hard": 2400}
BREAKS = ("\n", "。", "！", "？", ". ", "! ", "? ", "；", "; ")


@dataclass(frozen=True)
class Chunk:
    unit_version_ids: Tuple[str, ...]
    text: str
    chunk_id: str


def params_hash(params=None):
    return hashlib.sha256(canonical_bytes(params or DEFAULT_PARAMS)).hexdigest()


def _windows(text, hard):
    """Cut an over-long text at the last boundary inside each window of `hard` characters (never mid-way through a boundary mark)."""
    out, pos = [], 0
    while len(text) - pos > hard:
        window = text[pos:pos + hard]
        cut = max((window.rfind(b) + len(b) for b in BREAKS if window.rfind(b) > hard * 0.5), default=hard)
        out.append(text[pos:pos + cut])
        pos += cut
    out.append(text[pos:])
    return [w for w in out if w.strip()]


def chunk_units(units, params=None):
    """units: list of (unit_version_id, text, heading_path) in document order. Returns a list of Chunk."""
    params = params or DEFAULT_PARAMS
    limit, hard = params["limit"], params["hard"]
    ph = params_hash(params)
    chunks: List[Chunk] = []
    cur_ids: List[str] = []
    cur_texts: List[str] = []
    cur_len = 0
    cur_section = None

    def flush():
        nonlocal cur_ids, cur_texts, cur_len
        if cur_texts:
            text = "\n\n".join(cur_texts)
            chunks.append(Chunk(tuple(cur_ids), text, ids.chunk_id(cur_ids, NAME, VERSION, ph, ids.text_sha256(text))))
        cur_ids, cur_texts, cur_len = [], [], 0

    for uv, text, path in units:
        section = tuple(path)
        if not text.strip():
            continue
        if section != cur_section:
            flush()
            cur_section = section
        if len(text) > hard:
            flush()
            for piece in _windows(text, hard):
                cur_ids, cur_texts, cur_len = [uv], [piece], len(piece)
                flush()
            continue
        extra = len(text) + (2 if cur_texts else 0)
        if cur_texts and cur_len + extra > limit:
            flush()
            extra = len(text)
        cur_ids.append(uv)
        cur_texts.append(text)
        cur_len += extra
    flush()
    return chunks
