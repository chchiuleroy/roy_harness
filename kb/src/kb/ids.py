"""Identifiers (Gate 1 / Gate 2 section 4.1).

Random, stable: source_id, unit_id, proposal_id, commit_id, approval_id.
Deterministic (hash of a canonical preimage with a domain prefix): revision_id, unit_version_id, statement_id, chunk_id.

The preimage field list of each deterministic id IS the contract. Changing it means a new domain string
(kb.rev.v1 -> kb.rev.v2), never an edit in place; test_ids pins golden values so an accidental change fails loudly.
Every identifier that goes into a preimage must carry the prefix of the role it plays (a unit id cannot stand in for a source id).
"""
import base64
import hashlib
import hmac
import re
import secrets

from .canonical import canonical_bytes, nfc
from .errors import IdError

RANDOM_PREFIXES = {"src", "unit", "pr", "cm", "ap", "coll", "tomb", "conv"}
DET_PREFIXES = {"rev", "uv", "st", "ck"}
ID_RE = re.compile(r"^[a-z]{2,5}_[a-z2-7]{20,52}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

D_REVISION = "kb.rev.v1"
D_UNIT_VERSION = "kb.uv.v2"
D_STATEMENT = "kb.st.v3"
D_CHUNK = "kb.ck.v2"

# what a statement may be about: a source, a unit (or one of its versions), a chunk, or a collection
SUBJECT_PREFIXES = {"src", "unit", "uv", "ck", "coll"}


def new_id(prefix):
    if prefix not in RANDOM_PREFIXES:
        raise IdError(f"{prefix!r} is not a random-id prefix")
    return prefix + "_" + base64.b32encode(secrets.token_bytes(15)).decode("ascii").lower().rstrip("=")


def _digest(domain, fields):
    return hashlib.sha256(canonical_bytes([domain] + list(fields))).digest()


def det_id(prefix, domain, fields):
    if prefix not in DET_PREFIXES:
        raise IdError(f"{prefix!r} is not a deterministic-id prefix")
    return prefix + "_" + base64.b32encode(_digest(domain, fields)[:20]).decode("ascii").lower().rstrip("=")


def _need_id(value, what, prefixes):
    if not isinstance(value, str) or not ID_RE.match(value) or value.split("_", 1)[0] not in prefixes:
        raise IdError(f"{what} must be an identifier with prefix {sorted(prefixes)}: {value!r}")
    return value


def _need_hex(value, what):
    if not isinstance(value, str) or not HEX64_RE.match(value):
        raise IdError(f"{what} is not a sha256 hex digest: {value!r}")
    return value


_KEY = None
D_TEXT = b"kb.text.v1\0"
D_VALUE = b"kb.value.v1\0"
D_KEYCHECK = b"kb.keycheck.v1\0"


def use_key(key):
    """The key of the data folder in use (kb.keys loads it). One data folder per process: a second one replaces it, and the check value in each database
    refuses a database whose key is not the one loaded."""
    global _KEY
    if not isinstance(key, bytes) or len(key) != 32:
        raise IdError("the text key must be 32 bytes")
    _KEY = key


def _hmac(domain, data):
    if _KEY is None:
        raise IdError("no text key is loaded: every hash of text needs the key of its data folder (kb.keys)")
    return hmac.new(_KEY, domain + data, hashlib.sha256).hexdigest()


def key_check(key):
    """What the database keeps to tell its own key from another one; it reveals nothing of the key."""
    return hmac.new(key, D_KEYCHECK, hashlib.sha256).hexdigest()


def keyed_sha256(data):
    """HMAC-SHA256 under the text key of bytes (a canonical form of something that carries text). Same shape as a sha256 hex digest."""
    return _hmac(D_VALUE, data)


def text_sha256(text):
    """HMAC-SHA256, under the text key, of NFC-normalised UTF-8 text. Used for chunk text and as the base of fingerprints. Keyed so that a hash that
    stays in the database after a hide cannot be used to confirm a guess of the hidden text."""
    return _hmac(D_TEXT, nfc(text).encode("utf-8"))


def fingerprint(text):
    """Content fingerprint for anchors: identity of the text itself, never part of an id on its own."""
    return text_sha256(text)


def revision_id(source_id, blob_sha256):
    """Content-addressed: the same bytes of the same source are the same revision (A -> B -> A returns to A)."""
    return det_id("rev", D_REVISION, [_need_id(source_id, "source_id", {"src"}), _need_hex(blob_sha256, "blob_sha256")])


def unit_version_id(unit_id, conversion_output_sha256, locator, fp):
    return det_id("uv", D_UNIT_VERSION, [_need_id(unit_id, "unit_id", {"unit"}),
                                         _need_hex(conversion_output_sha256, "conversion_output_sha256"), locator,
                                         _need_hex(fp, "fingerprint")])


def statement_id(subject_id, key, value, origin, evidence):
    """Statements are immutable. A correction is a new statement that supersedes the old one, never an edit. The value and the evidence both can carry
    text (and both are erased after a hide), so they enter the preimage only as one keyed digest (ninth review: the evidence was plain)."""
    return det_id("st", D_STATEMENT, [_need_id(subject_id, "subject_id", SUBJECT_PREFIXES), key,
                                      keyed_sha256(canonical_bytes({"value": value, "evidence": evidence})), origin])


def chunk_id(unit_version_ids, chunker_name, chunker_version, params_hash, text_hash):
    """The unit versions are a SET: order and repetition never change the id (sorted and de-duplicated before hashing)."""
    uvs = sorted({_need_id(u, "unit_version_id", {"uv"}) for u in unit_version_ids})
    if not uvs:
        raise IdError("a chunk needs at least one unit version")
    return det_id("ck", D_CHUNK, [uvs, chunker_name, chunker_version, _need_hex(params_hash, "params_hash"),
                                  _need_hex(text_hash, "text_hash")])
