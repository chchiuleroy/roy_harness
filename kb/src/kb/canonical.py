"""Canonical bytes for hash preimages.

One rule everywhere an identifier or fingerprint is derived: JSON, keys sorted, no insignificant whitespace, UTF-8,
every string NFC-normalised, floats rejected (their text form is not a contract worth carrying into an identity).
"""
import json
import unicodedata

from .errors import CanonicalError


def nfc(text):
    return unicodedata.normalize("NFC", text)


def _norm(obj):
    if isinstance(obj, bool) or obj is None or isinstance(obj, int):
        return obj
    if isinstance(obj, str):
        return nfc(obj)
    if isinstance(obj, (list, tuple)):
        return [_norm(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonicalError(f"dict keys must be str, got {type(k).__name__}")
            nk = nfc(k)
            if nk in out:
                raise CanonicalError(f"keys collide after NFC normalisation: {nk!r}")
            out[nk] = _norm(v)
        return out
    raise CanonicalError(f"{type(obj).__name__} is not allowed in a canonical preimage")


def canonical_bytes(obj):
    try:
        return json.dumps(_norm(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except UnicodeEncodeError as e:      # a lone surrogate cannot become UTF-8
        raise CanonicalError(f"text is not encodable as UTF-8: {e}") from e
