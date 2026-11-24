"""Raw-evidence store (Gate 1 P1, WC01/02/13), append-only audit (P5), collection profiles (P7, T-F1 for the pieces that exist)."""
import ast
import hashlib
import os
import sqlite3
import unittest

from kb import audit, cas, colls, db, ids
from kb.errors import AuditError, BlobError, KbError, PolicyViolation
from kb.profile import Profile, require_agent_visible, require_convert, require_external_model, require_index

from .support import DbTestCase, SRC, run_py, tmpdir

CAS_SRC = os.path.join(SRC, "kb", "cas.py")


class Base(DbTestCase):
    def setUp(self):
        super().setUp()
        self.d = tmpdir(self)
        self.db_path = os.path.join(self.d, "kb.db")
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        self.store = cas.BlobStore(os.path.join(self.d, "blobs"))

    def write(self, name, data):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(data)
        return p


class TestBlobStore(Base):
    def test_copy_mode_stores_by_hash_and_reads_back(self):
        """P1: the stored bytes are exactly the input, found by their own hash."""
        data = b"hello evidence" * 1000
        ref = self.store.put_bytes(self.conn, data)
        self.assertEqual(ref.sha256, hashlib.sha256(data).hexdigest())
        self.assertTrue(ref.created)
        with open(self.store.blob_path(ref.sha256), "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(self.store.verify(self.conn, ref.sha256), cas.OK)

    def test_same_bytes_twice_is_one_blob(self):
        """WC13: duplicates must not double count or double store."""
        a = self.store.put_path(self.conn, self.write("a.bin", b"same"))
        b = self.store.put_path(self.conn, self.write("b.bin", b"same"))
        self.assertTrue(a.created)
        self.assertFalse(b.created)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM blobs").fetchone()[0], 1)
        stored = [f for _, _, fs in os.walk(self.store.dir) for f in fs if len(f) == 64]
        self.assertEqual(len(stored), 1)

    def test_blobs_are_read_only_on_disk(self):
        """P1: an accidental write through the file system must fail, not rewrite evidence."""
        ref = self.store.put_bytes(self.conn, b"x")
        with self.assertRaises(PermissionError):
            open(self.store.blob_path(ref.sha256), "ab")

    def test_ref_mode_records_without_copying_and_detects_change(self):
        """WC01: the original stays where it is; if it changes, verify says so instead of serving stale text."""
        p = self.write("orig.txt", b"version one")
        ref = self.store.put_path(self.conn, p, mode="ref")
        self.assertEqual(ref.mode, "ref")
        self.assertFalse(os.path.exists(self.store.blob_path(ref.sha256)))
        self.assertEqual(self.store.verify(self.conn, ref.sha256), cas.OK)
        with open(p, "wb") as f:
            f.write(b"version two")
        self.assertEqual(self.store.verify(self.conn, ref.sha256), cas.MISMATCH)

    def test_ref_source_moved_is_reported_missing(self):
        """WC02: 'source unavailable' must be an explicit state, never a silent success."""
        p = self.write("orig.txt", b"moving")
        ref = self.store.put_path(self.conn, p, mode="ref")
        os.rename(p, p + ".moved")
        self.assertEqual(self.store.verify(self.conn, ref.sha256), cas.MISSING)

    def test_damaged_copy_is_detected_and_repaired_by_a_new_put(self):
        """P1: bit rot or tampering is found by verify; putting the good bytes again restores them."""
        ref = self.store.put_bytes(self.conn, b"precious")
        path = self.store.blob_path(ref.sha256)
        os.chmod(path, 0o666)
        with open(path, "wb") as f:
            f.write(b"corrupt!")
        self.assertEqual(self.store.verify(self.conn, ref.sha256), cas.MISMATCH)
        again = self.store.put_bytes(self.conn, b"precious")
        self.assertEqual(again.sha256, ref.sha256)
        self.assertEqual(self.store.verify(self.conn, ref.sha256), cas.OK)

    def test_storing_a_blob_syncs_its_directory(self):
        """A new directory entry must be made durable, not only the file contents (process-kill tests cannot see the difference)."""
        calls = []
        original = cas.fsync_dir
        cas.fsync_dir = lambda p: calls.append(os.path.abspath(p))
        self.addCleanup(setattr, cas, "fsync_dir", original)
        ref = self.store.put_bytes(self.conn, b"durable please")
        self.assertIn(os.path.abspath(os.path.dirname(self.store.blob_path(ref.sha256))), calls)
        self.assertIn(os.path.abspath(self.store.dir), calls)              # the fan-out directory was new

    def test_unknown_blob_and_bad_input_are_errors(self):
        with self.assertRaises(BlobError):
            self.store.verify(self.conn, "0" * 64)
        with self.assertRaises(BlobError):
            self.store.put_path(self.conn, os.path.join(self.d, "missing.bin"))
        with self.assertRaises(BlobError):
            self.store.put_path(self.conn, self.write("x", b"x"), mode="link")

    def test_kill_after_temp_write_leaves_no_blob_and_recover_cleans_the_temp(self):
        """T-C1 analogue for evidence: a crash mid-write must not leave a partial file under a real blob name."""
        src = self.write("big.bin", os.urandom(200000))
        code = (f"from kb import cas,db;s=cas.BlobStore({os.path.join(self.d, 'blobs')!r});c=db.connect({self.db_path!r});"
                f"s.put_path(c,{src!r})")
        r = run_py(code, {"KB_FAULT": "cas.after_temp_write"})
        self.assertEqual(r.returncode, 137, r.stderr[-300:])
        finals = [f for _, _, fs in os.walk(self.store.dir) for f in fs if len(f) == 64]
        self.assertEqual(finals, [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM blobs").fetchone()[0], 0)
        self.assertGreaterEqual(self.store.recover(), 1)
        self.assertEqual(os.listdir(self.store.tmp), [])

    def test_kill_between_file_and_row_is_repaired_by_putting_again(self):
        """A blob file without its row is harmless; the next put must complete the record without duplicating the file."""
        src = self.write("f.bin", b"row comes second")
        code = (f"from kb import cas,db;s=cas.BlobStore({os.path.join(self.d, 'blobs')!r});c=db.connect({self.db_path!r});"
                f"s.put_path(c,{src!r})")
        self.assertEqual(run_py(code, {"KB_FAULT": "cas.before_row"}).returncode, 137)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM blobs").fetchone()[0], 0)
        ref = self.store.put_path(self.conn, src)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM blobs").fetchone()[0], 1)
        self.assertEqual(self.store.verify(self.conn, ref.sha256), cas.OK)

    def test_there_is_no_delete_api_and_only_temp_files_are_ever_removed(self):
        """Gate 1 decisions 3 and 4: evidence is never removed by this module. Pinned by name and by syntax tree."""
        for name in dir(cas.BlobStore):
            self.assertFalse(any(w in name.lower() for w in ("delete", "remove", "unlink", "purge")), name)
        with open(CAS_SRC, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        offenders = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for call in [c for c in ast.walk(fn) if isinstance(c, ast.Call)]:
                f = call.func
                dotted = (f.value.id + "." + f.attr) if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
                if dotted in ("os.remove", "os.unlink", "shutil.rmtree", "os.rmdir") and fn.name != "_discard_temp":
                    offenders.append((fn.name, dotted))
        self.assertEqual(offenders, [])


class TestAudit(Base):
    def test_accepts_identifiers_digests_ints_and_tokens(self):
        audit.append(self.conn, "blob_put", {"sha256": "a" * 64, "mode": "copy", "size": 12, "source": ids.new_id("src")})
        ev = audit.events(self.conn)
        self.assertEqual(ev[0][2], "blob_put")

    def test_free_text_cannot_enter_the_log(self):
        """P5: the log keeps events and ids, not original text or diffs; a careless call must fail, not leak."""
        for bad in ({"note": "the quarterly numbers are 12.4"}, {"path": "C:/Users/roy/secret.txt"}, {"x": "src_short"},
                    {"x": "a" * 65}, {"x": -1}, {"x": 1.5}, {"x": None}, {"x": ["a"]}):
            with self.assertRaises(AuditError, msg=str(bad)):
                audit.append(self.conn, "doctor", bad)
        with self.assertRaises(AuditError):
            audit.append(self.conn, "doctor", {"Bad Key": 1})
        with self.assertRaises(AuditError):
            audit.append(self.conn, "not_an_event", {})
        for not_a_mapping in ([], "", (), 0, ["a"]):                 # falsey non-mappings used to become {} silently
            with self.assertRaises(AuditError, msg=repr(not_a_mapping)):
                audit.append(self.conn, "doctor", not_a_mapping)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM audit_events").fetchone()[0], 0)
        audit.append(self.conn, "doctor", None)                      # None means no ids

    def test_log_is_append_only_in_the_database(self):
        audit.append(self.conn, "init", {})
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM audit_events")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE audit_events SET type='doctor'")


class TestProfile(Base):
    def test_defaults_are_conservative(self):
        """Decision 5: until Roy sets a policy per scenario, nothing is sent out and no agent sees it."""
        p = Profile()
        self.assertFalse(p.agent_visible)
        self.assertFalse(p.allow_external_model)
        self.assertEqual(p.sensitivity, "private")

    def test_secret_cannot_be_loosened_by_flags(self):
        """A per-flag mistake must not turn a secret collection into an indexed, agent-visible one."""
        for flags in ({"index_plaintext": True}, {"allow_external_model": True}, {"agent_visible": True}):
            args = dict(sensitivity="secret", index_plaintext=False, allow_external_model=False, agent_visible=False)
            args.update(flags)
            with self.assertRaises(KbError, msg=str(flags)):
                Profile(**args)
        Profile(sensitivity="secret", index_plaintext=False)           # the strict form is fine

    def test_strict_loading_rejects_typos_and_bad_types(self):
        """A misspelt key ('agent_visble') silently ignored would leave the default in force while the owner thinks otherwise."""
        for bad in ('{"agent_visble": true}', '{"retention_days": 0}', '{"retention_days": true}', '{"sensitivity": "top"}',
                    '{"index_plaintext": "yes"}', '[]', 'not json', '{"allowed_converters": "pdf"}'):
            with self.assertRaises(KbError, msg=bad):
                Profile.from_json(bad)

    def test_json_roundtrip(self):
        p = Profile(sensitivity="public", agent_visible=True, retention_days=30, allowed_converters=("pdf", "text"))
        self.assertEqual(Profile.from_json(p.to_json()), p)

    def test_enforcement_points_refuse_and_allow(self):
        """T-F1 for the points that exist now: each one is a hard stop, not a hint."""
        closed = Profile(allowed_converters=("text",))
        with self.assertRaises(PolicyViolation):
            require_convert(closed, "pdf")
        require_convert(closed, "text")
        require_convert(Profile(), "anything")
        with self.assertRaises(PolicyViolation):
            require_index(Profile(sensitivity="secret", index_plaintext=False))
        require_index(Profile())
        with self.assertRaises(PolicyViolation):
            require_external_model(Profile())
        require_external_model(Profile(allow_external_model=True))
        with self.assertRaises(PolicyViolation):
            require_agent_visible(Profile())
        require_agent_visible(Profile(agent_visible=True))

    def test_collection_stores_and_reloads_its_profile(self):
        p = Profile(sensitivity="public", agent_visible=True)
        cid = colls.create(self.conn, "papers", p)
        self.assertEqual(colls.get_profile(self.conn, cid), p)

    def test_a_tampered_stored_profile_fails_closed_on_load(self):
        """Someone edits profile_json by hand to loosen a secret collection: loading must raise, not serve."""
        cid = colls.create(self.conn, "diary", Profile(sensitivity="secret", index_plaintext=False))
        self.conn.execute("UPDATE collections SET profile_json=? WHERE collection_id=?",
                          ('{"sensitivity":"secret","agent_visible":true,"index_plaintext":false,"allow_external_model":false}', cid))
        with self.assertRaises(KbError):
            colls.get_profile(self.conn, cid)
        with self.assertRaises(KbError):
            colls.get_profile(self.conn, "coll_" + "a" * 20)


if __name__ == "__main__":
    unittest.main()
