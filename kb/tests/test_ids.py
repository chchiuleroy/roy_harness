"""T-ID1: identifier contract. Why each test exists is in its docstring: an id that drifts silently breaks reruns,
de-duplication, index rebuilds and manifest reconciliation without any single test going red."""
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import unicodedata
import unittest

from kb import ids
from kb.canonical import canonical_bytes
from kb.errors import CanonicalError, IdError

A = "src_" + "a" * 20          # a source
B = "src_" + "b" * 20
U1 = "unit_" + "a" * 20        # units
U2 = "unit_" + "b" * 20
V1 = "uv_" + "a" * 20          # unit versions
V2 = "uv_" + "b" * 20
H1 = "1" * 64
H2 = "2" * 64
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
KEY = bytes(range(32))                # a fixed key for the pinned values; a real data folder makes its own (kb.keys)


def independent(prefix, domain, fields):
    """Re-implementation of the documented contract using only json and hashlib (no kb code)."""
    raw = json.dumps([domain] + fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return prefix + "_" + base64.b32encode(hashlib.sha256(raw).digest()[:20]).decode().lower().rstrip("=")


def independent_keyed(key, domain, data):
    """The keyed digest of the contract with only hmac and hashlib: HMAC-SHA256 under the key of domain + data."""
    return hmac.new(key, domain + data, hashlib.sha256).hexdigest()


class TestDeterministicIds(unittest.TestCase):
    def setUp(self):
        ids.use_key(KEY)

    def test_matches_documented_contract(self):
        """The preimage rule in the docstring is the contract; an independent implementation must agree."""
        self.assertEqual(ids.revision_id(A, H1), independent("rev", "kb.rev.v1", [A, H1]))
        self.assertEqual(ids.chunk_id([V2, V1], "heading", "1", H1, H2),
                         independent("ck", "kb.ck.v2", [[V1, V2], "heading", "1", H1, H2]))
        value_digest = independent_keyed(KEY, b"kb.value.v1\0", json.dumps({"value": "Hello", "evidence": {"conv": "x"}}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        self.assertEqual(ids.statement_id(A, "title", "Hello", "native", {"conv": "x"}),
                         independent("st", "kb.st.v3", [A, "title", value_digest, "native"]))
        self.assertEqual(ids.text_sha256("Hello"), independent_keyed(KEY, b"kb.text.v1\0", b"Hello"))

    def test_golden_values_pin_the_preimage(self):
        """Pinned literals: any accidental change of a preimage field list or encoding fails here, loudly."""
        self.assertEqual(ids.revision_id(A, H1), "rev_" + GOLDEN["rev"])
        self.assertEqual(ids.statement_id(A, "title", "Hello", "native", {"conv": "x"}), "st_" + GOLDEN["st"])

    def test_same_input_same_id_across_processes(self):
        """Reruns must reproduce ids; hash randomisation or process state must not leak into them."""
        code = ("from kb import ids;ids.use_key(bytes(range(32)));print(ids.revision_id('%s','%s'), ids.statement_id('%s','k','v','native',{'a':1}))" % (A, H1, A))
        outs = set()
        for seed in ("1", "2", "random"):
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=SRC, PYTHONUTF8="1")
            outs.add(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True).stdout)
        self.assertEqual(len(outs), 1)
        self.assertEqual(outs.pop().split()[0], ids.revision_id(A, H1))

    def test_every_preimage_field_matters(self):
        """A field left out of the preimage would let two different things share one identity."""
        base = ids.unit_version_id(U1, H1, {"page": 1}, H2)
        self.assertNotEqual(base, ids.unit_version_id(U2, H1, {"page": 1}, H2))
        self.assertNotEqual(base, ids.unit_version_id(U1, H2, {"page": 1}, H2))
        self.assertNotEqual(base, ids.unit_version_id(U1, H1, {"page": 2}, H2))
        self.assertNotEqual(base, ids.unit_version_id(U1, H1, {"page": 1}, H1))
        st = ids.statement_id(A, "k", "v", "native", {"e": 1})
        for args in [(B, "k", "v", "native", {"e": 1}), (A, "k2", "v", "native", {"e": 1}), (A, "k", "v2", "native", {"e": 1}),
                     (A, "k", "v", "human", {"e": 1}), (A, "k", "v", "native", {"e": 2})]:
            self.assertNotEqual(st, ids.statement_id(*args))
        ck = ids.chunk_id([V1], "heading", "1", H1, H2)
        for args in [([V2], "heading", "1", H1, H2), ([V1], "window", "1", H1, H2), ([V1], "heading", "2", H1, H2),
                     ([V1], "heading", "1", H2, H2), ([V1], "heading", "1", H1, H1)]:
            self.assertNotEqual(ck, ids.chunk_id(*args))
        self.assertNotEqual(ids.revision_id(A, H1), ids.revision_id(B, H1))
        self.assertNotEqual(ids.revision_id(A, H1), ids.revision_id(A, H2))

    def test_domains_are_isolated(self):
        """Different kinds of object with identical-looking fields must never collide, and a domain bump is a new id space."""
        fields = [A, H1]
        self.assertNotEqual(ids.det_id("rev", "kb.rev.v1", fields), ids.det_id("rev", "kb.rev.v2", fields))
        self.assertNotEqual(ids.det_id("rev", "kb.rev.v1", fields), ids.det_id("uv", "kb.uv.v1", fields))

    def test_a_hash_of_text_needs_the_key_and_changes_with_it(self):
        """Eighth review: a plain hash of hidden text that stays in the database lets whoever holds the database confirm a guess. A hash made with the key
        cannot be made, or compared, without it."""
        plain = hashlib.sha256("secret paragraph".encode("utf-8")).hexdigest()
        keyed = ids.fingerprint("secret paragraph")
        self.assertNotEqual(keyed, plain)
        self.assertEqual(len(keyed), 64)
        sid = ids.statement_id(A, "text", "secret paragraph", "native", {"conv": "x"})
        ids.use_key(bytes(32))
        self.assertNotEqual(ids.fingerprint("secret paragraph"), keyed)
        self.assertNotEqual(ids.statement_id(A, "text", "secret paragraph", "native", {"conv": "x"}), sid)
        self.assertNotEqual(ids.keyed_sha256(b"data"), independent_keyed(KEY, b"kb.value.v1\0", b"data"))

    def test_the_evidence_is_keyed_too(self):
        """Ninth review: evidence can carry text and is erased after a hide, so it must not stay in the preimage as plain data."""
        a = ids.statement_id(A, "k", "v", "native", {"quote": "secret words"})
        ids.use_key(bytes(32))
        self.assertNotEqual(a, ids.statement_id(A, "k", "v", "native", {"quote": "secret words"}))
        ids.use_key(KEY)
        self.assertEqual(a, ids.statement_id(A, "k", "v", "native", {"quote": "secret words"}))
        self.assertNotEqual(a, ids.statement_id(A, "k", "v", "native", {"quote": "other words"}))

    def test_without_a_key_nothing_is_hashed(self):
        """A missing key must stop the work, never fall back to a plain hash (that would be the exposure this exists to close)."""
        saved, ids._KEY = ids._KEY, None
        try:
            for call in (lambda: ids.text_sha256("x"), lambda: ids.fingerprint("x"), lambda: ids.keyed_sha256(b"x"),
                         lambda: ids.statement_id(A, "k", "v", "native", None)):
                with self.assertRaises(IdError):
                    call()
        finally:
            ids._KEY = saved

    def test_the_key_must_be_32_bytes(self):
        for bad in (b"", b"x" * 31, b"x" * 33, "x" * 32, None):
            with self.assertRaises(IdError):
                ids.use_key(bad)

    def test_the_check_value_does_not_give_the_key_away_and_tells_keys_apart(self):
        self.assertNotIn(KEY.hex(), ids.key_check(KEY))
        self.assertNotEqual(ids.key_check(KEY), ids.key_check(bytes(32)))
        self.assertEqual(ids.key_check(KEY), independent_keyed(KEY, b"kb.keycheck.v1\0", b""))

    def test_field_boundaries_are_unambiguous(self):
        """Plain concatenation would let ('ab','c') equal ('a','bc'); structured preimages must not."""
        self.assertNotEqual(ids.chunk_id([V1], "ab", "c", H1, H2), ids.chunk_id([V1], "a", "bc", H1, H2))
        self.assertNotEqual(ids.statement_id(A, "ab", "c", "native", None), ids.statement_id(A, "a", "bc", "native", None))

    def test_chunk_unit_versions_are_a_set(self):
        """The same chunk assembled in a different order, or with a repeated member, is the same chunk."""
        self.assertEqual(ids.chunk_id([V1, V2], "h", "1", H1, H2), ids.chunk_id([V2, V1], "h", "1", H1, H2))
        self.assertEqual(ids.chunk_id([V1], "h", "1", H1, H2), ids.chunk_id([V1, V1], "h", "1", H1, H2))
        with self.assertRaises(IdError):
            ids.chunk_id([], "h", "1", H1, H2)

    def test_identifiers_must_carry_the_prefix_of_their_role(self):
        """A well-shaped id of the wrong kind (a unit where a source belongs) would build a valid-looking, meaningless identity."""
        with self.assertRaises(IdError):
            ids.revision_id(U1, H1)                    # a unit is not a source
        with self.assertRaises(IdError):
            ids.unit_version_id(A, H1, {}, H2)         # a source is not a unit
        with self.assertRaises(IdError):
            ids.chunk_id([U1], "h", "1", H1, H2)       # a unit is not a unit version
        with self.assertRaises(IdError):
            ids.statement_id("rev_" + "a" * 20, "k", "v", "native", None)   # statements are about sources, units, chunks, collections
        ids.statement_id(U1, "k", "v", "native", None)
        ids.statement_id(V1, "k", "v", "native", None)

    def test_nfc_equivalence(self):
        """The same text typed composed or decomposed must not fork an identity."""
        composed, decomposed = "café", unicodedata.normalize("NFD", "café")
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(ids.statement_id(A, "k", composed, "native", None), ids.statement_id(A, "k", decomposed, "native", None))
        self.assertEqual(ids.text_sha256(composed), ids.text_sha256(decomposed))

    def test_dict_key_order_is_irrelevant(self):
        """Evidence and locator dicts are built by different code paths; insertion order must not change an identity."""
        e1 = {"conv": "x", "page": 3, "bbox": [1, 2]}
        e2 = {"bbox": [1, 2], "page": 3, "conv": "x"}
        self.assertEqual(ids.statement_id(A, "k", "v", "native", e1), ids.statement_id(A, "k", "v", "native", e2))
        self.assertEqual(ids.unit_version_id(U1, H1, e1, H2), ids.unit_version_id(U1, H1, e2, H2))

    def test_types_do_not_blur(self):
        """1, '1' and True are different values; floats are refused outright."""
        a = ids.statement_id(A, "k", 1, "native", None)
        self.assertNotEqual(a, ids.statement_id(A, "k", "1", "native", None))
        self.assertNotEqual(a, ids.statement_id(A, "k", True, "native", None))
        with self.assertRaises(CanonicalError):
            ids.statement_id(A, "k", 1.5, "native", None)
        with self.assertRaises(CanonicalError):
            canonical_bytes({1: "x"})
        with self.assertRaises(CanonicalError):
            canonical_bytes("lone \ud800 surrogate")

    def test_nfc_key_collision_is_an_error(self):
        """Two keys that normalise to the same text would silently drop one of them."""
        with self.assertRaises(CanonicalError):
            canonical_bytes({"café": 1, unicodedata.normalize("NFD", "café"): 2})

    def test_bad_shapes_are_refused(self):
        """A truncated or mistyped digest must not become part of an identity."""
        for bad in ("abc", "g" * 64, "1" * 63, None, 5):
            with self.assertRaises(IdError):
                ids.revision_id(A, bad)
        for bad in ("src", "SRC_" + "a" * 20, "src_" + "1" * 20, "", None):
            with self.assertRaises(IdError):
                ids.revision_id(bad, H1)


class TestRandomIds(unittest.TestCase):
    def test_shape_and_uniqueness(self):
        """Random ids must be unique in practice and recognisable by shape (the audit log relies on the shape)."""
        seen = {ids.new_id("src") for _ in range(20000)}
        self.assertEqual(len(seen), 20000)
        self.assertTrue(all(ids.ID_RE.match(i) for i in seen))

    def test_prefixes_are_not_interchangeable(self):
        """A deterministic prefix asked for as random (or the reverse) is a programming error."""
        with self.assertRaises(IdError):
            ids.new_id("rev")
        with self.assertRaises(IdError):
            ids.det_id("src", "x", [])


GOLDEN = {"rev": "3wjhn4ek3qbdr3m6pd2rsspdljvbr3de", "st": "dzhwpubigb7dfuf3t7ehvywzbhevuqs2"}

if __name__ == "__main__":
    unittest.main()
