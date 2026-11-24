"""Schema, migrations (T-M1..T-M4), database-enforced rules, single writer (T-W1)."""
import os
import sqlite3
import time
import unittest

from kb import db, home
from kb.errors import BackupInvalid, KbError, MigrationFailed, SchemaTooNew, WriterBusy
from kb.faults import fault_point
from kb.writer import WriterLock

from .support import DbTestCase, db_fingerprint, run_py, tmpdir

EXPECTED_TABLES = {"schema_migrations", "collections", "sources", "source_locations", "blobs", "revisions", "revision_observations",
                   "conversions", "units", "unit_versions", "unit_lineage", "statements", "patches", "chunks", "commits",
                   "commit_units", "commit_statements", "commit_chunks", "active_commit", "proposals", "approvals", "tombstones",
                   "audit_events", "intake_skips", "conversion_units", "chunk_memo", "scrub_window", "text_key_check"}


LATEST = db.MIGRATIONS[-1].version        # the real schema moves on; tests derive version numbers from it
NEXT = LATEST + 1


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def make_v1(testcase):
    d = tmpdir(testcase)
    path = os.path.join(d, "kb.db")
    db.migrate(path)
    return d, path


def m2_good(conn):
    conn.execute("ALTER TABLE collections ADD COLUMN note TEXT")
    fault_point(f"migrate.{NEXT}.mid")
    conn.execute("UPDATE collections SET note = 'x'")


def m2_bad(conn):
    conn.execute("ALTER TABLE collections ADD COLUMN note TEXT")
    conn.execute("UPDATE collections SET note = 'half' WHERE name = 'c1'")
    raise ValueError("data transform failed on row c2")


def seed(conn):
    for i in (1, 2, 3):
        conn.execute("INSERT INTO collections VALUES (?,?,?,?)", (f"coll_{i}", f"c{i}", "{}", "2026-01-01T00:00:00Z"))


M2_GOOD = db.MIGRATIONS + [db.Migration(NEXT, "note", m2_good)]
M2_BAD = db.MIGRATIONS + [db.Migration(NEXT, "note_bad", m2_bad)]


class TestSchema(DbTestCase):
    def test_fresh_database_has_every_table_and_pragmas(self):
        """Gate 2 4.2 is the design; a missing table would only surface when the stage that needs it starts."""
        d, path = make_v1(self)
        conn = db.connect(path)
        self.assertEqual(tables(conn), EXPECTED_TABLES)
        self.assertEqual(db.schema_version(conn), LATEST)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_foreign_keys_are_enforced(self):
        """A source pointing at a collection that does not exist is a bug that must stop at the database."""
        d, path = make_v1(self)
        conn = db.connect(path)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO sources VALUES ('src_x','nope','file','t')")

    def test_identity_rows_cannot_be_deleted_or_rewritten(self):
        """Gate 1 P1/P5: evidence and history are append-only; the rule must hold for any code path, not only today's."""
        d, path = make_v1(self)
        conn = db.connect(path)
        conn.execute("INSERT INTO blobs VALUES (?, 5, 'copy', 'blobs/aa/x', 't')", ("a" * 64,))
        for sql in ("DELETE FROM blobs", "UPDATE blobs SET size = 6", "DELETE FROM audit_events", "UPDATE audit_events SET type = 'x'"):
            conn.execute("INSERT INTO audit_events(ts,type,ids_json) VALUES ('t','e','{}')")
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                conn.execute(sql)

    def test_statement_content_is_frozen_but_state_can_move(self):
        """A correction is a new statement; only the lifecycle state of an existing one may change."""
        d, path = make_v1(self)
        conn = db.connect(path)
        conn.execute("INSERT INTO statements(statement_id,subject_id,key,value_json,origin,evidence_json,state) "
                     "VALUES ('st_a','unit_a','title','\"x\"','native','{}','candidate')")
        conn.execute("UPDATE statements SET state='committed' WHERE statement_id='st_a'")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE statements SET value_json='\"y\"' WHERE statement_id='st_a'")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM statements")

    def test_commit_members_freeze_after_staging(self):
        """Gate 2 F25: a published commit must read the same forever; membership is the record of what it contained."""
        d, path = make_v1(self)
        conn = db.connect(path)
        conn.execute("INSERT INTO collections VALUES ('coll_1','c','{}','t')")
        conn.execute("INSERT INTO sources VALUES ('src_1','coll_1','file','t')")
        conn.execute("INSERT INTO units VALUES ('unit_1','t')")
        conn.execute("INSERT INTO unit_versions VALUES ('uv_1','unit_1',NULL,'p','{}','f')")
        conn.execute("INSERT INTO commits VALUES ('cm_1',NULL,NULL,'staging','t')")
        conn.execute("INSERT INTO commit_units VALUES ('cm_1','uv_1')")
        conn.execute("UPDATE commits SET state='verified' WHERE commit_id='cm_1'")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM commit_units")
        conn.execute("INSERT INTO units VALUES ('unit_2','t')")
        conn.execute("INSERT INTO unit_versions VALUES ('uv_2','unit_2',NULL,'p','{}','f')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO commit_units VALUES ('cm_1','uv_2')")

    def test_members_cannot_be_moved_into_or_out_of_a_frozen_commit(self):
        """An UPDATE of commit_id is a delete from one commit and an insert into another; both ends must be in staging, or a published
        commit could be emptied by moving its members elsewhere."""
        d, path = make_v1(self)
        conn = db.connect(path)
        conn.execute("INSERT INTO units VALUES ('unit_1','t')")
        conn.execute("INSERT INTO unit_versions VALUES ('uv_1','unit_1',NULL,'p','{}','f')")
        conn.execute("INSERT INTO unit_versions VALUES ('uv_2','unit_1',NULL,'p','{}','g')")
        for c in ("cm_1", "cm_2", "cm_3"):
            conn.execute("INSERT INTO commits VALUES (?,NULL,NULL,'staging','t')", (c,))
        conn.execute("INSERT INTO commit_units VALUES ('cm_1','uv_1')")
        conn.execute("INSERT INTO commit_units VALUES ('cm_3','uv_2')")
        conn.execute("UPDATE commit_units SET commit_id='cm_2' WHERE commit_id='cm_3'")          # staging to staging is fine
        conn.execute("UPDATE commits SET state='verified' WHERE commit_id='cm_1'")
        with self.assertRaises(sqlite3.IntegrityError):                                           # leaving a frozen commit
            conn.execute("UPDATE commit_units SET commit_id='cm_2' WHERE commit_id='cm_1'")
        with self.assertRaises(sqlite3.IntegrityError):                                           # joining a frozen commit
            conn.execute("UPDATE commit_units SET commit_id='cm_1' WHERE commit_id='cm_2'")
        self.assertEqual(conn.execute("SELECT commit_id FROM commit_units WHERE unit_version_id='uv_1'").fetchone()[0], "cm_1")

    def test_sources_and_units_are_identity_rows(self):
        """A source cannot be re-homed or re-typed and a unit's creation cannot be rewritten by a stray UPDATE."""
        d, path = make_v1(self)
        conn = db.connect(path)
        conn.execute("INSERT INTO collections VALUES ('coll_1','a','{}','t')")
        conn.execute("INSERT INTO collections VALUES ('coll_2','b','{}','t')")
        conn.execute("INSERT INTO sources VALUES ('src_1','coll_1','file','t')")
        conn.execute("INSERT INTO units VALUES ('unit_1','t')")
        for sql in ("UPDATE sources SET collection_id='coll_2'", "UPDATE sources SET kind='url'", "UPDATE units SET created_at='x'",
                    "DELETE FROM sources", "DELETE FROM units"):
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                conn.execute(sql)

    def test_only_one_active_commit(self):
        """Two active commits would make 'what does the system serve' ambiguous."""
        d, path = make_v1(self)
        conn = db.connect(path)
        conn.execute("INSERT INTO commits VALUES ('cm_1',NULL,NULL,'active','t')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO commits VALUES ('cm_2',NULL,NULL,'active','t')")
        conn.execute("INSERT INTO active_commit VALUES (1,'cm_1')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO active_commit VALUES (2,'cm_1')")


class TestMigrations(DbTestCase):
    def test_rerun_is_a_noop(self):
        """T-M4: starting twice must be safe; startup always calls migrate."""
        d, path = make_v1(self)
        self.assertEqual(db.migrate(path), [])
        self.assertEqual(db.migrate(path, M2_GOOD), [NEXT])
        self.assertEqual(db.migrate(path, M2_GOOD), [])

    def test_step_by_step_from_old_schema(self):
        """T-M4: an old database moves forward one numbered step at a time and keeps its data."""
        d, path = make_v1(self)
        conn = db.connect(path)
        seed(conn)
        conn.close()
        db.migrate(path, M2_GOOD, backup_dir=os.path.join(d, "backups"))
        conn = db.connect(path)
        self.assertEqual(conn.execute("SELECT count(*), min(note) FROM collections").fetchone(), (3, "x"))
        self.assertEqual(db.schema_version(conn), NEXT)
        self.assertTrue(os.listdir(os.path.join(d, "backups")))

    def test_newer_schema_is_refused_without_change(self):
        """T-M4: old code must not 'repair' a database written by newer code."""
        d, path = make_v1(self)
        db.migrate(path, M2_GOOD)
        before = db_fingerprint(db.connect(path))
        with self.assertRaises(SchemaTooNew):
            db.migrate(path)       # code that only knows v1
        self.assertEqual(db_fingerprint(db.connect(path)), before)

    def test_gap_in_numbering_is_refused(self):
        """A skipped number means a migration was lost; refuse before touching anything."""
        d, path = make_v1(self)
        with self.assertRaises(MigrationFailed):
            db.migrate(path, [db.MIGRATIONS[0], db.Migration(3, "x", m2_good)])

    def test_failed_transform_rolls_everything_back(self):
        """T-M2: schema change plus half-done data change must vanish together; the version must not move."""
        d, path = make_v1(self)
        conn = db.connect(path)
        seed(conn)
        before = db_fingerprint(conn)
        conn.close()
        with self.assertRaises(MigrationFailed):
            db.migrate(path, M2_BAD, backup_dir=os.path.join(d, "backups"))
        conn = db.connect(path)
        self.assertEqual(db_fingerprint(conn), before)
        self.assertNotIn("note", [r[1] for r in conn.execute("PRAGMA table_info(collections)")])
        self.assertEqual(db.schema_version(conn), LATEST)

    def test_damaged_backup_blocks_the_migration(self):
        """T-M3: if the safety net is broken, do not jump: refuse and leave the database exactly as it was."""
        d, path = make_v1(self)
        conn = db.connect(path)
        seed(conn)
        before = db_fingerprint(conn)
        conn.close()
        original = db._make_backup
        self.addCleanup(setattr, db, "_make_backup", original)

        def damaged(conn, dest):
            original(conn, dest)
            with open(dest, "r+b") as f:
                f.seek(0)
                f.write(b"NOT A SQLITE FILE" * 10)
        db._make_backup = damaged
        with self.assertRaises(BackupInvalid):
            db.migrate(path, M2_GOOD, backup_dir=os.path.join(d, "backups"))
        self.assertEqual(db_fingerprint(db.connect(path)), before)

    def test_restore_refuses_a_damaged_backup_and_accepts_a_good_one(self):
        """T-M3: restoring from a bad file would turn a failed migration into data loss."""
        d, path = make_v1(self)
        conn = db.connect(path)
        seed(conn)
        good = os.path.join(d, "good.db")
        db._make_backup(conn, good)
        conn.close()
        bad = os.path.join(d, "bad.db")
        with open(bad, "wb") as f:
            f.write(b"garbage" * 100)
        with self.assertRaises(BackupInvalid):
            db.restore_backup(bad, path)
        db.restore_backup(good, path)
        self.assertEqual(db.connect(path).execute("SELECT count(*) FROM collections").fetchone()[0], 3)

    def _kill_during(self, point, migrations_expr, seeded):
        d, path = make_v1(self) if seeded else (tmpdir(self), None)
        path = path or os.path.join(d, "kb.db")
        if seeded:
            c = db.connect(path)
            seed(c)
            c.close()
        code = (f"import sys;sys.path.insert(0,{os.path.dirname(os.path.abspath(__file__))!r});"
                f"from tests.test_db import {migrations_expr};from kb import db;db.migrate({path!r},{migrations_expr},backup_dir={os.path.join(d, 'bk')!r})")
        r = run_py(f"import sys;sys.path.insert(0,{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r});" + code,
                   {"KB_FAULT": point})
        self.assertEqual(r.returncode, 137, r.stderr[-400:])
        return d, path

    def test_kill_at_every_step_of_first_migration_leaves_clean_v0(self):
        """T-M1: a crash at any step of creating the schema leaves an empty v0 database, never a half-built one."""
        for point in ("migrate.begin", "migrate.1.after_apply", "migrate.before_commit"):
            with self.subTest(point=point):
                d, path = self._kill_during(point, "M2_GOOD", seeded=False)
                conn = db.connect(path)
                self.assertEqual(db.schema_version(conn), 0)
                self.assertEqual(tables(conn), set())
                db.migrate(path)                       # and it recovers by simply running again
                self.assertEqual(tables(db.connect(path)), EXPECTED_TABLES)

    def test_kill_in_the_middle_of_a_data_migration_leaves_old_schema_and_data(self):
        """T-M1: killed between the schema change and the data change: still the old schema, old data, then recoverable."""
        d, path = self._kill_during(f"migrate.{NEXT}.mid", "M2_GOOD", seeded=True)
        conn = db.connect(path)
        self.assertEqual(db.schema_version(conn), LATEST)
        self.assertNotIn("note", [r[1] for r in conn.execute("PRAGMA table_info(collections)")])
        self.assertEqual(conn.execute("SELECT count(*) FROM collections").fetchone()[0], 3)
        conn.close()
        db.migrate(path, M2_GOOD, backup_dir=os.path.join(d, "bk2"))
        self.assertEqual(db.schema_version(db.connect(path)), NEXT)


class TestRealMigrationFromV1(DbTestCase):
    """The first real upgrade (v1 to v2, intake_skips): an existing database with data must keep it and gain the new table."""

    def make_v1_with_data(self):
        d = tmpdir(self)
        path = os.path.join(d, "kb.db")
        db.migrate(path, db.MIGRATIONS[:1])
        c = db.connect(path)
        seed(c)
        c.close()
        return d, path

    def test_upgrade_keeps_data_and_adds_the_table(self):
        d, path = self.make_v1_with_data()
        self.assertEqual(db.migrate(path, backup_dir=os.path.join(d, "bk")), list(range(2, LATEST + 1)))
        c = db.connect(path)
        self.assertEqual(db.schema_version(c), LATEST)
        self.assertEqual(c.execute("SELECT count(*) FROM collections").fetchone()[0], 3)
        self.assertIn("intake_skips", tables(c))
        self.assertTrue(os.listdir(os.path.join(d, "bk")))

    def test_kill_during_the_real_upgrade_leaves_v1_whole_and_rerun_finishes(self):
        d, path = self.make_v1_with_data()
        code = f"from kb import db;db.migrate({path!r},backup_dir={os.path.join(d, 'bk')!r})"
        self.assertEqual(run_py(code, {"KB_FAULT": "migrate.2.after_apply"}).returncode, 137)
        c = db.connect(path)
        self.assertEqual(db.schema_version(c), 1)
        self.assertNotIn("intake_skips", tables(c))
        self.assertEqual(c.execute("SELECT count(*) FROM collections").fetchone()[0], 3)
        c.close()
        db.migrate(path, backup_dir=os.path.join(d, "bk"))
        self.assertIn("intake_skips", tables(db.connect(path)))


class TestHome(unittest.TestCase):
    def test_no_default_location(self):
        """A hard-coded default folder once pointed a sibling tool at the wrong data; kb must refuse to guess."""
        old = os.environ.pop("KB_HOME", None)
        self.addCleanup(lambda: old is not None and os.environ.__setitem__("KB_HOME", old))
        with self.assertRaises(KbError):
            home.resolve_home(None)

    def test_init_creates_layout_and_is_idempotent(self):
        d = tmpdir(self)
        h = home.Home(os.path.join(d, "h"))
        self.assertEqual(h.init(), list(range(1, LATEST + 1)))
        self.assertEqual(h.init(), [])
        for s in home.SUBDIRS:
            self.assertTrue(os.path.isdir(h.sub(s)))


class TestWriterLock(unittest.TestCase):
    HOLDER = ("import sys,time;from kb.writer import WriterLock;l=WriterLock(sys.argv[1]).acquire();"
              "print('locked',flush=True);time.sleep(60)")

    def _start_holder(self, d):
        import subprocess
        import sys
        from .support import SRC
        env = dict(os.environ, PYTHONPATH=SRC, PYTHONUTF8="1")
        p = subprocess.Popen([sys.executable, "-c", self.HOLDER, d], stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "locked")
        return p

    def test_second_writer_is_refused_and_lock_dies_with_its_holder(self):
        """T-W1: two writers corrupt shared state; and a crashed holder must not block the next start forever."""
        d = tmpdir(self)
        p = self._start_holder(d)
        with self.assertRaises(WriterBusy):
            WriterLock(d).acquire()
        p.kill()
        p.wait()
        lock = WriterLock(d)
        for _ in range(50):                       # the OS releases the lock when the process is gone
            try:
                lock.acquire()
                break
            except WriterBusy:
                time.sleep(0.1)
        else:
            self.fail("lock was not released after the holder died")
        lock.release()

    def test_same_process_cannot_take_it_twice(self):
        """A second acquire in one process would silently give two 'writers' in one program."""
        d = tmpdir(self)
        with WriterLock(d):
            with self.assertRaises(WriterBusy):
                WriterLock(d).acquire()

    def test_readers_are_not_blocked_by_an_open_write_transaction(self):
        """T-W1: the CLI search and the resident server must read while a commit is being written."""
        d, path = make_v1(self)
        w = db.connect(path)
        w.execute("BEGIN IMMEDIATE")
        w.execute("INSERT INTO collections VALUES ('coll_1','c','{}','t')")
        r = db.connect(path, readonly=True)
        self.assertEqual(r.execute("SELECT count(*) FROM collections").fetchone()[0], 0)   # sees the committed state only
        w.execute("COMMIT")
        self.assertEqual(r.execute("SELECT count(*) FROM collections").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
