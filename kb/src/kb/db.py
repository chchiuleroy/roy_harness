"""SQLite access and schema migrations (Gate 2 sections 4.2 and 7: T-M1..T-M4).

Version truth is PRAGMA user_version, changed inside the same transaction as the schema work, so a migration is
all-or-nothing. Before touching an existing database a backup is made with SQLite's backup API and verified; a backup that
fails its integrity check blocks the migration (nothing is attempted, nothing changes). kb never downgrades.
"""
import os
import sqlite3
import time
from typing import Callable, List, NamedTuple

from . import ids, keys, schema
from .errors import BackupInvalid, MigrationFailed, SchemaTooNew
from .faults import fault_point


class Migration(NamedTuple):
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def connect(path, readonly=False):
    """Autocommit mode (transactions are explicit), foreign keys on, WAL so readers never wait for the writer."""
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=5.0)
    else:
        conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        keys.bind(conn, None if path in ("", ":memory:") else path)
    except BaseException:
        conn.close()
        raise
    return conn


def schema_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def execute_script(conn, statements):
    """Run statements one by one (executescript would COMMIT the surrounding transaction)."""
    for s in statements:
        conn.execute(s)


def _apply_v1(conn):
    execute_script(conn, schema.V1_STATEMENTS)
    conn.execute("INSERT INTO schema_migrations(version, name) VALUES (1, 'init')")


def _apply_v2(conn):
    """Paths the intake refused or could not read yet, so that nothing is silently missing from the index (Gate 1 P4, H5)."""
    execute_script(conn, schema.V2_STATEMENTS)
    conn.execute("INSERT INTO schema_migrations(version, name) VALUES (2, 'intake_skips')")


def _apply_v3(conn):
    """Which unit versions make up each conversion, in document order."""
    execute_script(conn, schema.V3_STATEMENTS)
    conn.execute("INSERT INTO schema_migrations(version, name) VALUES (3, 'conversion_units')")


def _apply_v4(conn):
    """Review rules enforced by the database, decided_at on proposals, patch provenance, the chunk memo (stage S2)."""
    execute_script(conn, schema.V4_STATEMENTS)
    conn.execute("INSERT INTO schema_migrations(version, name) VALUES (4, 'review_rules')")


def _apply_v5(conn):
    """An index for the position lookup of a search hit (stage S3: found by the midterm latency measurement)."""
    execute_script(conn, schema.V5_STATEMENTS)
    conn.execute("INSERT INTO schema_migrations(version, name) VALUES (5, 'conversion_units_by_unit_version')")


def _apply_v6(conn):
    """The scrub window: derived text of hidden things can be erased, only through commit.scrub_hidden_text (stage S4, H2)."""
    execute_script(conn, schema.V6_STATEMENTS)
    conn.execute("INSERT INTO schema_migrations(version, name) VALUES (6, 'scrub_window')")


def _apply_v7(conn):
    """Hashes of text are made with a key from now on (stage S4, eighth review). Ids made without a key cannot be turned into ids made with one, so a data
    folder that already holds sources is refused: rebuild it in a new folder (the original files are kept in blobs/)."""
    if conn.execute("SELECT 1 FROM sources LIMIT 1").fetchone():
        raise MigrationFailed("this data folder was made before hashes of text were keyed and cannot be upgraded: make a new data folder and add the "
                              "original files again (they are kept in blobs/)")
    file = conn.execute("PRAGMA database_list").fetchone()[2]
    key = keys.ensure(file or None, replace_damaged=True)
    execute_script(conn, schema.V7_STATEMENTS)
    conn.execute("INSERT INTO text_key_check(id, check_value) VALUES (1, ?)", (ids.key_check(key),))
    conn.execute("INSERT INTO schema_migrations(version, name) VALUES (7, 'text_key')")


def _apply_v8(conn):
    """The locator of a hidden unit can be erased, only inside the scrub window (stage S4, ninth review). The structural keys and statement ids that exist
    were made without keyed headings and evidence and cannot be turned into new ones, so a folder that already holds sources is refused, as in migration 7
    (tenth review); the schema 7 existed for hours and no real folder was made with it."""
    if conn.execute("SELECT 1 FROM sources LIMIT 1").fetchone():
        raise MigrationFailed("this data folder was made before the headings of a unit were kept out of its keys and cannot be upgraded: make a new data folder "
                              "and add your original files again")
    execute_script(conn, schema.V8_STATEMENTS)
    conn.execute("INSERT INTO schema_migrations(version, name) VALUES (8, 'locator_scrub')")


MIGRATIONS: List[Migration] = [Migration(1, "init", _apply_v1), Migration(2, "intake_skips", _apply_v2),
                               Migration(3, "conversion_units", _apply_v3), Migration(4, "review_rules", _apply_v4),
                               Migration(5, "conversion_units_by_unit_version", _apply_v5), Migration(6, "scrub_window", _apply_v6), Migration(7, "text_key", _apply_v7),
                               Migration(8, "locator_scrub", _apply_v8)]


def integrity_ok(path):
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            c.close()
    except sqlite3.Error:
        return False


def _make_backup(conn, dest):
    """Separate function so tests can replace it with one that produces a damaged file."""
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
    finally:
        out.close()


def restore_backup(backup_path, db_path):
    """Replace db_path with a backup, refusing a backup that fails its integrity check."""
    if not integrity_ok(backup_path):
        raise BackupInvalid(f"backup {backup_path} failed its integrity check; not restoring")
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    os.replace(backup_path, db_path)


def migrate(path, migrations=None, backup_dir=None):
    """Bring the database at `path` to the latest version. Returns the list of versions applied (empty if already current)."""
    migrations = list(MIGRATIONS if migrations is None else migrations)
    latest = migrations[-1].version
    if [m.version for m in migrations] != list(range(1, latest + 1)):
        raise MigrationFailed("migrations must be numbered 1..N without gaps")
    conn = connect(path)
    try:
        current = schema_version(conn)
        if current > latest:
            raise SchemaTooNew(f"database is schema {current}, this kb knows up to {latest}; kb never downgrades")
        if current == latest:
            return []
        if current > 0:
            backup_dir = backup_dir or os.path.join(os.path.dirname(os.path.abspath(path)), "backups")
            os.makedirs(backup_dir, exist_ok=True)
            dest = os.path.join(backup_dir, f"pre_v{current + 1}_{time.strftime('%Y%m%dT%H%M%S')}.db")
            _make_backup(conn, dest)
            if not integrity_ok(dest):
                raise BackupInvalid(f"pre-migration backup {dest} failed its integrity check; migration not attempted")
        applied = []
        for m in migrations[current:]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                fault_point("migrate.begin")
                m.apply(conn)
                fault_point(f"migrate.{m.version}.after_apply")
                conn.execute(f"PRAGMA user_version={int(m.version)}")
                fault_point("migrate.before_commit")
                conn.execute("COMMIT")
            except BaseException as e:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                if isinstance(e, KeyboardInterrupt):
                    raise
                raise MigrationFailed(f"migration {m.version} ({m.name}) failed and was rolled back: {e}") from e
            applied.append(m.version)
        return applied
    finally:
        conn.close()
