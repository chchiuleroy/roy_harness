"""kb doctor (OPT6: missing capability is a report line, never a crash) and the command line."""
import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stderr

from kb import cas, cli, db, doctor, ids, keys
from kb.home import Home

from .support import DbTestCase, tmpdir


class TestCapabilities(unittest.TestCase):
    def test_everything_missing_is_reported_with_impact_not_raised(self):
        """OPT6: with nothing installed the system must still report, and say what each gap costs."""
        caps = doctor.check_capabilities(which=lambda n: None, find_spec=lambda n: None, environ={})
        by = {c.name: c for c in caps}
        self.assertFalse(by["numpy"].available)
        self.assertEqual(by["numpy"].affects, ("dense retrieval",))
        self.assertFalse(by["embedding_server"].available)
        self.assertFalse(by["pdf_pymupdf"].available)
        self.assertFalse(by["ffmpeg"].available)
        self.assertFalse(by["speech_recognition"].available)
        self.assertTrue(by["sqlite_fts5"].available)             # this one is provided by Python itself

    def test_environment_variables_locate_tools_without_hard_coded_paths(self):
        d = tmpdir(self)
        server, model = os.path.join(d, "srv.exe"), os.path.join(d, "m.gguf")
        for p in (server, model):
            open(p, "w").close()
        caps = doctor.check_capabilities(which=lambda n: None, find_spec=lambda n: object(),
                                         environ={"KB_LLAMA_SERVER": server, "KB_EMBED_MODEL": model})
        by = {c.name: c for c in caps}
        self.assertTrue(by["embedding_server"].available)
        self.assertTrue(by["numpy"].available)

    def test_embedding_needs_both_server_and_model(self):
        d = tmpdir(self)
        server = os.path.join(d, "srv.exe")
        open(server, "w").close()
        caps = doctor.check_capabilities(which=lambda n: None, find_spec=lambda n: None, environ={"KB_LLAMA_SERVER": server})
        self.assertFalse({c.name: c for c in caps}["embedding_server"].available)


class TestHomeHealth(DbTestCase):
    def setUp(self):
        super().setUp()
        self.home = Home(os.path.join(tmpdir(self), "h"))
        self.home.init()

    def test_fresh_home_is_healthy(self):
        self.assertEqual(doctor.check_home(self.home, deep=True), [])

    def test_missing_database_is_a_problem(self):
        self.assertEqual(doctor.check_home(Home(os.path.join(tmpdir(self), "nothing")))[0][0], "db_missing")

    def test_deep_check_finds_a_corrupted_blob_and_a_missing_one(self):
        """P1 in practice: bit rot is found by the health check, not by the day someone needs the file."""
        conn = db.connect(self.home.db_path)
        store = cas.BlobStore(self.home.sub("blobs"))
        a = store.put_bytes(conn, b"alpha")
        b = store.put_bytes(conn, b"beta")
        self.assertEqual(doctor.check_home(self.home, deep=True), [])
        pa = store.blob_path(a.sha256)
        os.chmod(pa, 0o666)
        with open(pa, "wb") as f:
            f.write(b"rotten")
        os.chmod(store.blob_path(b.sha256), 0o666)
        os.remove(store.blob_path(b.sha256))
        kinds = sorted(p[0] for p in doctor.check_home(self.home, deep=True))
        self.assertEqual(kinds, ["blob_hash_mismatch", "blob_missing"])
        self.assertEqual(doctor.check_home(self.home, deep=False), [])       # the cheap check does not re-hash

    def test_old_schema_and_leftover_temp_are_reported(self):
        d = tmpdir(self)
        h = Home(os.path.join(d, "old"))
        os.makedirs(os.path.join(h.path, "blobs", "_tmp"))
        with open(os.path.join(h.path, "blobs", "_tmp", "x"), "w") as f:
            f.write("partial")
        c = db.connect(h.db_path)
        c.execute("PRAGMA user_version=0")
        c.close()
        kinds = {p[0] for p in doctor.check_home(h)}
        self.assertEqual(kinds, {"schema_version", "leftover_temp"})


class TestTextKeyStatus(DbTestCase):
    """`kb doctor` reports the state of the text key. Before, a folder whose key was missing or not its own made the doctor raise (db.connect refuses it),
    so the tool meant to say what is wrong could not say it. A lost key is the one damage that cannot be repaired (ids are made from it)."""

    def setUp(self):
        super().setUp()
        self.home = Home(os.path.join(tmpdir(self), "h"))
        self.home.init()
        self.key_path = keys.path_for(self.home.db_path)

    def test_a_good_key_is_reported_ok_and_never_printed(self):
        state, detail = doctor.key_state(self.home)
        self.assertEqual(state, "ok")
        with open(self.key_path, "rb") as f:
            key = f.read()
        rep = doctor.report(self.home)
        self.assertEqual(rep["text_key"]["state"], "ok")
        self.assertTrue(rep["healthy"])
        self.assertNotIn(key.hex(), json.dumps(rep))                 # the report reveals where the key is, not what it is

    def test_a_missing_key_is_a_reported_problem_not_an_exception(self):
        os.remove(self.key_path)
        problems = doctor.check_home(self.home, deep=True)           # used to raise KeyProblem
        self.assertEqual([p[0] for p in problems], ["text_key_missing"])
        self.assertIn("were not run", problems[0][1])                # says that the other checks could not run, so "one problem" is not read as "only one"
        rep = doctor.report(self.home)
        self.assertFalse(rep["healthy"])
        self.assertEqual(rep["text_key"]["state"], "missing")

    def test_a_key_of_the_wrong_size_is_damaged(self):
        with open(self.key_path, "wb") as f:
            f.write(b"short")
        self.assertEqual(doctor.key_state(self.home)[0], "damaged")
        self.assertEqual([p[0] for p in doctor.check_home(self.home)], ["text_key_damaged"])

    def test_the_key_of_another_folder_is_a_mismatch(self):
        with open(self.key_path, "wb") as f:
            f.write(os.urandom(32))
        self.assertEqual(doctor.key_state(self.home)[0], "mismatch")
        self.assertEqual([p[0] for p in doctor.check_home(self.home)], ["text_key_mismatch"])

    def test_a_check_table_without_a_row_is_damaged(self):
        """db.connect refuses this database too (it would otherwise leave the key of another folder in use), so the doctor must say so rather than call it fine."""
        conn = sqlite3.connect(self.home.db_path)
        conn.execute("DELETE FROM text_key_check")
        conn.commit()
        conn.close()
        self.assertEqual(doctor.key_state(self.home)[0], "damaged")
        self.assertEqual([p[0] for p in doctor.check_home(self.home)], ["text_key_damaged"])

    def test_a_file_that_is_not_a_database_is_reported_not_raised(self):
        """sqlite raises DatabaseError, not OperationalError, for this; the doctor is the tool that has to say 'unreadable'."""
        with open(self.home.db_path, "wb") as f:
            f.write(b"this is not a sqlite file" * 100)
        self.assertEqual(doctor.key_state(self.home)[0], "unreadable")
        self.assertEqual([p[0] for p in doctor.check_home(self.home)], ["text_key_unreadable"])

    def test_a_folder_without_a_database_is_not_called_a_key_problem(self):
        h = Home(os.path.join(tmpdir(self), "nothing"))
        self.assertEqual(doctor.key_state(h)[0], "no_database")
        self.assertEqual([p[0] for p in doctor.check_home(h)], ["db_missing"])

    def test_a_database_older_than_the_key_has_nothing_to_report(self):
        conn = db.connect(self.home.db_path)
        conn.execute("DROP TABLE text_key_check")
        conn.close()
        self.assertEqual(doctor.key_state(self.home)[0], "not_applicable")

    def test_reading_the_state_loads_no_key_into_the_process(self):
        """db.connect binds the key of the folder it opens; the doctor looks at a folder that may be someone else's and must leave the key in use alone."""
        ids.use_key(b"\x07" * 32)
        before = ids.text_sha256("probe")
        self.assertEqual(doctor.key_state(self.home)[0], "ok")        # the folder's own, good key: the path that could load it
        self.assertEqual(ids.text_sha256("probe"), before)
        with open(self.key_path, "wb") as f:
            f.write(os.urandom(32))
        doctor.key_state(self.home)                                   # and a key that is not this folder's
        doctor.report(self.home)
        self.assertEqual(ids.text_sha256("probe"), before)


class TestCli(unittest.TestCase):
    def run_cli(self, argv, env=None):
        old = {k: os.environ.get(k) for k in (env or {})}
        os.environ.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stderr(err):
                code = cli.main(argv, out=out)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, out.getvalue(), err.getvalue()

    def test_no_home_is_an_error_not_a_guess(self):
        old = os.environ.pop("KB_HOME", None)
        self.addCleanup(lambda: old is not None and os.environ.__setitem__("KB_HOME", old))
        code, out, err = self.run_cli(["status"])
        self.assertEqual(code, 2)
        self.assertIn("no data folder", err)

    def test_init_status_doctor_flow(self):
        d = os.path.join(tmpdir(self), "h")
        code, out, _ = self.run_cli(["--home", d, "init"])
        self.assertEqual(code, 0)
        self.assertIn(str(list(range(1, db.MIGRATIONS[-1].version + 1))), out)
        code, out, _ = self.run_cli(["--home", d, "init"])
        self.assertIn("none (already current)", out)
        code, out, _ = self.run_cli(["status"], env={"KB_HOME": d})
        self.assertIn(f"schema_version: {db.MIGRATIONS[-1].version}", out)
        self.assertIn("blobs: 0", out)
        code, out, _ = self.run_cli(["--home", d, "doctor"])
        self.assertEqual(code, 0)
        self.assertIn("data folder: healthy", out)

    def test_doctor_prints_the_key_state_and_exits_1_when_the_key_is_missing(self):
        d = os.path.join(tmpdir(self), "h")
        self.run_cli(["--home", d, "init"])
        code, out, _ = self.run_cli(["--home", d, "doctor"])
        self.assertEqual(code, 0)
        self.assertIn("text key: ok", out)
        os.remove(keys.path_for(Home(d).db_path))
        code, out, err = self.run_cli(["--home", d, "doctor"])
        self.assertEqual(code, 1, err)
        self.assertIn("text key: missing", out)
        self.assertIn("text_key_missing", out)
        code, out, _ = self.run_cli(["--home", d, "doctor", "--json"])
        self.assertEqual(json.loads(out)["text_key"]["state"], "missing")

    def test_doctor_exit_code_reflects_problems(self):
        """Scripts rely on the exit code; a damaged folder must not exit 0."""
        d = os.path.join(tmpdir(self), "empty")
        code, out, _ = self.run_cli(["--home", d, "doctor"])
        self.assertEqual(code, 1)
        self.assertIn("db_missing", out)


if __name__ == "__main__":
    unittest.main()
