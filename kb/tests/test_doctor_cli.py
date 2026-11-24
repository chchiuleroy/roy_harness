"""kb doctor (OPT6: missing capability is a report line, never a crash) and the command line."""
import io
import os
import unittest
from contextlib import redirect_stderr

from kb import cas, cli, db, doctor
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

    def test_doctor_exit_code_reflects_problems(self):
        """Scripts rely on the exit code; a damaged folder must not exit 0."""
        d = os.path.join(tmpdir(self), "empty")
        code, out, _ = self.run_cli(["--home", d, "doctor"])
        self.assertEqual(code, 1)
        self.assertIn("db_missing", out)


if __name__ == "__main__":
    unittest.main()
