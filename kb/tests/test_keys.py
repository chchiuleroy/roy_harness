"""The text key (eighth review): every hash of text is made with a key that is not in the database. A lost or wrong key must stop the work out loud."""
import os
import sqlite3

from kb import db, ids, keys
from kb.home import Home

from .support import DbTestCase, tmpdir


class KeyWorld(DbTestCase):
    def make_home(self, name="h"):
        home = Home(os.path.join(tmpdir(self), name))
        home.init()
        return home

    def key_of(self, home):
        with open(keys.path_for(home.db_path), "rb") as f:
            return f.read()


class TestAFreshDataFolder(KeyWorld):
    def test_it_gets_a_key_of_32_bytes_next_to_the_database_and_not_in_the_backups(self):
        home = self.make_home()
        self.assertEqual(keys.path_for(home.db_path), os.path.join(home.path, "text.key"))
        self.assertEqual(len(self.key_of(home)), 32)
        self.assertEqual(os.listdir(home.sub("backups")), [])                        # a copy of the backups does not carry the key

    def test_the_database_keeps_a_check_value_and_never_the_key(self):
        home = self.make_home()
        conn = db.connect(home.db_path)
        (check,) = conn.execute("SELECT check_value FROM text_key_check").fetchone()
        self.assertEqual(check, ids.key_check(self.key_of(home)))
        with open(home.db_path, "rb") as f:
            self.assertNotIn(self.key_of(home), f.read())

    def test_two_data_folders_have_two_keys(self):
        self.assertNotEqual(self.key_of(self.make_home("a")), self.key_of(self.make_home("b")))

    def test_the_key_is_loaded_for_the_folder_that_was_connected_last(self):
        """One data folder per process, but a connection re-binds its own key, so going back to the first folder gives the first key again."""
        a, b = self.make_home("a"), self.make_home("b")
        db.connect(a.db_path)
        in_a = ids.fingerprint("same text")
        db.connect(b.db_path)
        in_b = ids.fingerprint("same text")
        self.assertNotEqual(in_a, in_b)
        db.connect(a.db_path)
        self.assertEqual(ids.fingerprint("same text"), in_a)

    def test_a_read_only_connection_loads_the_key_too(self):
        home = self.make_home()
        db.connect(home.db_path)
        expected = ids.fingerprint("x")
        other = self.make_home("other")
        db.connect(other.db_path)
        db.connect(home.db_path, readonly=True)
        self.assertEqual(ids.fingerprint("x"), expected)


class TestAKeyThatIsMissingWrongOrDamaged(KeyWorld):
    def test_a_missing_key_is_refused_and_not_made_again(self):
        home = self.make_home()
        os.remove(keys.path_for(home.db_path))
        with self.assertRaises(keys.KeyProblem) as ctx:
            db.connect(home.db_path)
        self.assertIn("missing", str(ctx.exception))
        self.assertIn("blobs/", str(ctx.exception))                                    # what to do: rebuild from the originals
        self.assertFalse(os.path.exists(keys.path_for(home.db_path)))                  # no quiet new key: it would give other ids

    def test_another_key_is_refused(self):
        home = self.make_home()
        with open(keys.path_for(home.db_path), "wb") as f:
            f.write(bytes(32))
        with self.assertRaises(keys.KeyProblem) as ctx:
            db.connect(home.db_path)
        self.assertIn("does not belong", str(ctx.exception))

    def test_a_key_of_the_wrong_size_is_refused(self):
        home = self.make_home()
        with open(keys.path_for(home.db_path), "wb") as f:
            f.write(b"short")
        with self.assertRaises(keys.KeyProblem) as ctx:
            db.connect(home.db_path)
        self.assertIn("damaged", str(ctx.exception))

    def test_a_refused_connection_does_not_stay_open(self):
        home = self.make_home()
        os.remove(keys.path_for(home.db_path))
        opened = []
        real = sqlite3.connect
        try:
            sqlite3.connect = lambda *a, **k: opened.append(real(*a, **k)) or opened[-1]
            with self.assertRaises(keys.KeyProblem):
                db.connect(home.db_path)
        finally:
            sqlite3.connect = real
        for c in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                c.execute("SELECT 1")                                                    # closed


class TestEnsure(KeyWorld):
    def test_an_existing_key_is_kept_never_replaced(self):
        home = self.make_home()
        before = self.key_of(home)
        keys.ensure(home.db_path)
        self.assertEqual(self.key_of(home), before)

    def test_a_database_that_lives_in_memory_gets_a_key_in_memory(self):
        key = keys.ensure(None)
        self.assertEqual(len(key), 32)
        self.assertEqual(ids.fingerprint("x"), ids.fingerprint("x"))


class TestAnOldDataFolderIsRefused(KeyWorld):
    def test_a_folder_with_sources_made_before_keyed_hashes_is_not_upgraded(self):
        d = tmpdir(self)
        path = os.path.join(d, "kb.db")
        db.migrate(path, db.MIGRATIONS[:6], backup_dir=os.path.join(d, "bk"))              # as it was before this change
        conn = db.connect(path)
        conn.execute("INSERT INTO collections(collection_id, name, profile_json, created_at) VALUES ('coll_aaaaaaaaaaaaaaaaaaaa', 'c', '{}', 't')")
        conn.execute("INSERT INTO sources(source_id, collection_id, kind, created_at) VALUES ('src_aaaaaaaaaaaaaaaaaaaa', 'coll_aaaaaaaaaaaaaaaaaaaa', 'file', 't')")
        conn.close()
        with self.assertRaises(db.MigrationFailed) as ctx:
            db.migrate(path, backup_dir=os.path.join(d, "bk"))
        self.assertIn("new data folder", str(ctx.exception))
        conn = db.connect(path)
        self.assertEqual(db.schema_version(conn), 6)                                      # rolled back, nothing changed
        self.assertFalse(os.path.exists(keys.path_for(path)))                             # and no key was made for it

    def test_an_empty_old_folder_is_upgraded(self):
        d = tmpdir(self)
        path = os.path.join(d, "kb.db")
        db.migrate(path, db.MIGRATIONS[:6], backup_dir=os.path.join(d, "bk"))
        self.assertEqual(db.migrate(path, backup_dir=os.path.join(d, "bk")), [7, 8])
        with open(keys.path_for(path), "rb") as f:
            self.assertEqual(len(f.read()), 32)


class TestOnlyAMissingTableMeansNoKey(KeyWorld):
    """A database that cannot be read (locked, damaged) must not be taken for one made before keys existed: the key of another folder would stay in use."""

    def test_a_locked_database_is_an_error_not_a_skipped_check(self):
        class Locked:
            def execute(self, *_):
                raise sqlite3.OperationalError("database is locked")
        with self.assertRaises(sqlite3.OperationalError):
            keys.bind(Locked(), os.path.join(tmpdir(self), "kb.db"))

    def test_a_missing_table_is_a_database_before_schema_7(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        keys.bind(conn, os.path.join(tmpdir(self), "kb.db"))                          # returns, loads nothing, raises nothing


class TestTheKeyCheckAndTheKeyFile(KeyWorld):
    def test_a_check_table_without_a_row_is_refused_not_skipped(self):
        """Ninth review: a database that looks keyed but holds no check value must not keep using the key of another folder."""
        home = self.make_home()
        conn = sqlite3.connect(home.db_path, isolation_level=None)
        conn.execute("DELETE FROM text_key_check")
        conn.close()
        with self.assertRaises(keys.KeyProblem) as ctx:
            db.connect(home.db_path)
        self.assertIn("no check value", str(ctx.exception))

    def test_the_key_file_is_made_whole_and_leaves_nothing_behind(self):
        d = tmpdir(self)
        path = os.path.join(d, "text.key")
        self.assertTrue(keys._create_whole(path, bytes(range(32))))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), bytes(range(32)))
        self.assertEqual(os.listdir(d), ["text.key"])                                      # no temporary file

    def test_an_existing_key_file_is_never_replaced_by_the_atomic_write(self):
        d = tmpdir(self)
        path = os.path.join(d, "text.key")
        keys._create_whole(path, bytes(range(32)))
        self.assertFalse(keys._create_whole(path, bytes(32)))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), bytes(range(32)))
        self.assertEqual(os.listdir(d), ["text.key"])

    def test_a_write_that_fails_does_not_leave_a_short_key_file(self):
        from unittest import mock
        d = tmpdir(self)
        path = os.path.join(d, "text.key")
        with mock.patch.object(keys.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                keys._create_whole(path, bytes(range(32)))
        self.assertEqual(os.listdir(d), [])                                              # no key and no temporary file either (tenth review)

    def test_the_message_for_a_lost_key_does_not_promise_the_file_names(self):
        home = self.make_home()
        os.remove(keys.path_for(home.db_path))
        with self.assertRaises(keys.KeyProblem) as ctx:
            db.connect(home.db_path)
        self.assertIn("named by their hash", str(ctx.exception))

    def test_a_file_system_without_hard_links_still_gets_a_whole_key_file(self):
        """The data folder may live on exFAT or a network share, where `os.link` fails."""
        from unittest import mock
        d = tmpdir(self)
        path = os.path.join(d, "text.key")
        with mock.patch.object(keys.os, "link", side_effect=OSError("hard links are not supported")):
            self.assertTrue(keys._create_whole(path, bytes(range(32))))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), bytes(range(32)))
            self.assertFalse(keys._create_whole(path, bytes(32)))                              # and an existing key is still never replaced
            with open(path, "rb") as f:
                self.assertEqual(f.read(), bytes(range(32)))
        self.assertEqual(os.listdir(d), ["text.key"])

    def test_the_fallback_does_not_replace_a_key_that_appears_at_the_last_moment(self):
        """Tenth review: a rename would replace the key of the other process on POSIX; an exclusive create does not."""
        from unittest import mock
        d = tmpdir(self)
        path = os.path.join(d, "text.key")
        real_open = open

        def other_process_wins(file, mode="r", *a, **k):
            if file == path and "x" in mode:
                with real_open(path, "wb") as f:
                    f.write(bytes(range(32)))
            return real_open(file, mode, *a, **k)
        with mock.patch.object(keys.os, "link", side_effect=OSError("hard links are not supported")),                 mock.patch("builtins.open", other_process_wins):
            self.assertFalse(keys._create_whole(path, bytes(32)))
        with real_open(path, "rb") as f:
            self.assertEqual(f.read(), bytes(range(32)))
        self.assertEqual(os.listdir(d), ["text.key"])


class TestEnsureAfterAnInterruptedWrite(KeyWorld):
    def test_a_short_key_file_left_by_an_interrupted_write_is_made_again_by_the_migration_only(self):
        d = tmpdir(self)
        path = os.path.join(d, "kb.db")
        with open(keys.path_for(path), "wb") as f:
            f.write(b"half")
        with self.assertRaises(keys.KeyProblem):
            keys.ensure(path)                                                            # anything else refuses a damaged key
        db.migrate(path, backup_dir=os.path.join(d, "bk"))                               # the migration has not used it yet
        with open(keys.path_for(path), "rb") as f:
            self.assertEqual(len(f.read()), 32)
        db.connect(path)

    def test_a_key_that_vanishes_at_the_last_moment_is_a_key_problem_not_a_type_error(self):
        from unittest import mock
        d = tmpdir(self)
        path = os.path.join(d, "kb.db")
        with mock.patch.object(keys, "_create_whole", return_value=False):
            with self.assertRaises(keys.KeyProblem):
                keys.ensure(path)


class TestSchemaEightRefusesAFolderWithSources(KeyWorld):
    def test_a_schema_7_folder_with_a_source_is_refused_and_an_empty_one_is_upgraded(self):
        d = tmpdir(self)
        path = os.path.join(d, "kb.db")
        db.migrate(path, db.MIGRATIONS[:7], backup_dir=os.path.join(d, "bk"))
        conn = db.connect(path)
        conn.execute("INSERT INTO collections(collection_id, name, profile_json, created_at) VALUES ('coll_aaaaaaaaaaaaaaaaaaaa', 'c', '{}', 't')")
        conn.execute("INSERT INTO sources(source_id, collection_id, kind, created_at) VALUES ('src_aaaaaaaaaaaaaaaaaaaa', 'coll_aaaaaaaaaaaaaaaaaaaa', 'file', 't')")
        with self.assertRaises(db.MigrationFailed) as ctx:
            db.migrate(path, backup_dir=os.path.join(d, "bk"))
        self.assertIn("new data folder", str(ctx.exception))
        self.assertEqual(db.schema_version(db.connect(path)), 7)
        empty = os.path.join(tmpdir(self), "kb.db")
        db.migrate(empty, db.MIGRATIONS[:7])
        self.assertEqual(db.migrate(empty), [8])

    def test_a_failure_to_remove_the_temporary_file_does_not_replace_the_real_error(self):
        """Eleventh review."""
        from unittest import mock
        d = tmpdir(self)
        path = os.path.join(d, "text.key")
        with mock.patch.object(keys.os, "fsync", side_effect=OSError("disk full")),                 mock.patch.object(keys.os, "remove", side_effect=PermissionError("locked")):
            with self.assertRaises(OSError) as ctx:
                keys._create_whole(path, bytes(range(32)))
        self.assertIn("disk full", str(ctx.exception))
        for name in os.listdir(d):                                                         # clean up what the patched remove could not
            os.remove(os.path.join(d, name))

    def test_a_failure_to_remove_it_after_a_good_write_is_shown_because_the_file_holds_the_key(self):
        from unittest import mock
        d = tmpdir(self)
        path = os.path.join(d, "text.key")
        with mock.patch.object(keys.os, "remove", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                keys._create_whole(path, bytes(range(32)))
        for name in os.listdir(d):
            os.remove(os.path.join(d, name))


class TestWhyAnEmptySourcesTableMeansNoHeadingsAnywhere(KeyWorld):
    """Eleventh review: migrations 7 and 8 refuse a folder that holds sources. That is enough only if everything that can hold a heading cannot exist without a
    source, which the foreign keys say; this pins it, so that a future table with headings and no such chain is noticed."""

    def references(self, conn, table):
        return {r[2] for r in conn.execute(f"PRAGMA foreign_key_list({table})")}

    def test_every_table_with_a_locator_hangs_on_a_source_through_foreign_keys(self):
        conn = db.connect(self.make_home().db_path)
        self.assertIn("sources", self.references(conn, "revisions"))
        self.assertIn("revisions", self.references(conn, "conversions"))
        self.assertIn("conversions", self.references(conn, "unit_versions"))
        self.assertIn("conversions", self.references(conn, "conversion_units"))
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_a_locator_row_without_its_parents_is_refused(self):
        conn = db.connect(self.make_home().db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO conversions(conv_id, revision_id, converter, version, params_hash, exec_identity_json, status, created_at) "
                         "VALUES ('conv_aaaaaaaaaaaaaaaaaaaa', 'rev_aaaaaaaaaaaaaaaaaaaa', 'c', '1', 'h', '{}', 'ok', 't')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO unit_versions(unit_version_id, unit_id, conv_id, kind, locator_json, fingerprint) "
                         "VALUES ('uv_aaaaaaaaaaaaaaaaaaaa', 'unit_aaaaaaaaaaaaaaaaaaaa', 'conv_aaaaaaaaaaaaaaaaaaaa', 'paragraph', '{}', 'f')")
