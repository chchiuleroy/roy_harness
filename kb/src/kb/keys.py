"""The key that every hash of text is made with (eighth review, decision of Roy 2026-11-24: "keyed hash", all of it).

Why: after a hide the text is gone from the database, but a plain hash of it stayed in several identifiers (the fingerprint of a unit, the id of its
unit version, the id of its statements, the id of its chunks). Anyone who held the database and could guess the paragraph could confirm the guess
(measured, scratchpad probe of 2026-11-24). With a key that is not in the database, a hash can only be made by someone who has the key.

The key is 32 random bytes in `text.key` in the data folder, next to the database and NOT in `backups/`. It is not secret from the person who owns the
folder; it protects a copy of the database or of a backup that is taken without the key file. The database holds a check value (HMAC of a constant), so
a lost or wrong key is refused out loud instead of producing different ids quietly. A lost key cannot be made again: the original files are kept in
`blobs/`, so the knowledge base can be rebuilt in a new data folder.
"""
import os
import sqlite3

from . import ids
from .errors import KbError

NAME = "text.key"
SIZE = 32


class KeyProblem(KbError):
    """The text key is missing, damaged or does not belong to this database."""


def path_for(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), NAME)


def load(db_path):
    """The key bytes, or None when there is no key file. A file of the wrong size is an error, never a silently shorter key."""
    try:
        with open(path_for(db_path), "rb") as f:
            key = f.read()
    except FileNotFoundError:
        return None
    if len(key) != SIZE:
        raise KeyProblem(f"{path_for(db_path)} is damaged: {len(key)} bytes, expected {SIZE}")
    return key


def _create_whole(path, key):
    """Write the key to a temporary file in the same folder, flush it to the disk, and give it its name with a hard link, which fails when the name is taken:
    on that path the key file is either absent or whole (ninth review), and an existing key is never replaced. False when the name was taken.
    On a file system without hard links (exFAT, some network shares) the final name is created exclusively and written at once: a crash can leave a short
    file there, which is refused loudly ("damaged") and made again by the migration that has not yet used it, but a key made by another process is never
    replaced (tenth review: a rename would replace it on POSIX)."""
    tmp = f"{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    failed = False
    try:
        with open(tmp, "xb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            try:
                with open(path, "xb") as f:
                    f.write(key)
                    f.flush()
                    os.fsync(f.fileno())
                return True
            except FileExistsError:
                return False
    except BaseException:
        failed = True
        raise
    finally:
        _discard(tmp, failed)


def _discard(tmp, failed):
    """Remove the temporary file. A failure to remove it must not replace the error that is already on its way (`failed`, eleventh review), and a file that is
    already gone is nothing; when nothing else went wrong the failure is shown, because the file holds the key."""
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    except OSError:
        if not failed:
            raise


def ensure(db_path, replace_damaged=False):
    """Make the key of a new data folder (an existing file is kept: a key is never replaced), load it, and return it. `db_path` None or empty is a
    database that lives only in memory: its key is made and kept in memory. `replace_damaged` is for the migration that makes the key, before any
    data is keyed with it: a key file that is not 32 bytes is then the leftover of an interrupted write and is made again."""
    if not db_path:
        key = os.urandom(SIZE)
    else:
        try:
            key = load(db_path)
        except KeyProblem:
            if not replace_damaged:
                raise
            os.remove(path_for(db_path))
            key = None
        if key is None:
            key = os.urandom(SIZE)
            if not _create_whole(path_for(db_path), key):      # another process made it a moment ago
                key = load(db_path)
                if key is None:
                    raise KeyProblem(f"the text key {path_for(db_path)} was made and removed again at the same moment; run the command again")
    ids.use_key(key)
    return key


def bind(conn, db_path):
    """Load the key for a database that was made with one, and check that it is the right key. A database without a check value (older than schema 7,
    or one being migrated) is left to the migration."""
    try:
        row = conn.execute("SELECT check_value FROM text_key_check").fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):               # a database older than schema 7: the migration makes the key
            return
        raise                                       # a locked or damaged database is not "no key": a stale key of another folder must not stay in use
    if row is None:
        raise KeyProblem("the table of the key check exists but holds no check value: the database is damaged or was made by hand")
    key = load(db_path) if db_path else None
    if key is None:
        if not db_path:
            return                                  # an in-memory database was just made in this process: its key is already set
        raise KeyProblem(f"the text key {path_for(db_path)} is missing. Without it the ids of this knowledge base cannot be made again; "
                         "put the file back, or make a new data folder and add your original files again (kb keeps copies in blobs/, named by their hash, "
                         "so the file names and folders are not there)")
    if ids.key_check(key) != row[0]:
        raise KeyProblem(f"the text key {path_for(db_path)} does not belong to this database")
    ids.use_key(key)
