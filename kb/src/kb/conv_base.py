"""Converter contract (Gate 2 section 4.6): bytes in, normalised units out, with a report that says what happened.

A converter is a pure function of its input bytes and hints: the same bytes always give the same units (T-W2). It never touches the
network and never executes anything it reads (T-U): HTML is reduced to text, CSV cells stay text. The execution identity records
which converter, which version and which parameters produced a result, so a later upgrade can tell what needs redoing.
"""
import hashlib
import sys
from dataclasses import dataclass, field
from typing import List

from .canonical import canonical_bytes

MAX_INPUT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Unit:
    kind: str                     # paragraph | heading | code | table | list | row_group | text_block
    text: str
    locator: dict                 # where it came from in the source (type + position)
    heading_path: tuple = ()      # enclosing headings, outermost first


@dataclass
class ConversionResult:
    status: str                   # ok | partial | failed
    units: List[Unit] = field(default_factory=list)
    meta: dict = field(default_factory=dict)          # title, links, tags ... (only what supports filtering, tracing, locating)
    warnings: List[str] = field(default_factory=list)
    report: dict = field(default_factory=dict)        # counts and risk flags (W3: per-source conversion report)


def exec_identity(converter, params=None):
    """Who produced a result: converter name and version, the parameters, the Python it ran on."""
    ident = {"converter": converter.name, "version": converter.version, "params": params or {},
             "python": sys.version.split()[0]}
    ident["params_hash"] = hashlib.sha256(canonical_bytes(ident["params"])).hexdigest()
    return ident


def decode_text(data):
    """Bytes to text. UTF-8 (with or without BOM) and UTF-16 with BOM are trusted; anything else falls back to big5/cp950 and is
    reported as a warning because the guess can be wrong. Returns (text, encoding, warnings)."""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16", []
    try:
        text = data.decode("utf-8-sig")
        return text, "utf-8", []
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp950"), "cp950", ["not valid UTF-8; decoded as cp950 (Traditional Chinese Windows code page), which may be wrong"]
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8-replace", [
            "not valid UTF-8 or cp950; undecodable bytes were replaced with U+FFFD"]


def too_large(data):
    return len(data) > MAX_INPUT_BYTES


def failed(reason, **report):
    return ConversionResult("failed", report=dict(report, reason=reason), warnings=[reason])
