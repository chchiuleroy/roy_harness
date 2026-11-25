"""kb inventory: every class of file kb keeps, where it lives and how much of it there is (Gate 2 section 4.10).
Deletion levels (S2) are verified class by class against this list, so a class that is not listed here cannot be forgotten by a deletion."""
import os

CLASSES = [
    ("raw_blobs", "blobs", "Managed copies of original files (content addressed, read-only)"),
    ("index_segments", "segments", "Published commits: lexical index and manifest"),
    ("staging", "staging", "Commits being built (removed on failure or recovery)"),
    ("backups", "backups", "Database backups taken before migrations"),
    ("logs", "logs", "Logs (the embedding server's own log)"),
    ("vector_cache", "vectors", "Cached text vectors (after a commit only those of the active commit's chunks are kept)"),
    ("opened_copies", "opened", "Named copies of originals made by `kb open` (removed at the next commit once their source is hidden)"),
    ("embedder_state", "embedder", "Process id and port of the resident embedding server"),
    ("ask_answers", "ask/outbox", "Answers of `kb ask` with the full text of the passages the model was given (removed at the next commit once a source among them is hidden)"),
    ("ask_tickets", "ask/tickets", "Working folders of `kb ask` (the model's prompt and session; removed when the question has been answered)"),
]


def _walk(path):
    files = size = 0
    for d, _, names in os.walk(path):
        for n in names:
            files += 1
            try:
                size += os.path.getsize(os.path.join(d, n))
            except OSError:
                pass
    return files, size


def inventory(home):
    out = []
    for name, sub, why in CLASSES:
        p = home.sub(sub)
        files, size = _walk(p) if os.path.isdir(p) else (0, 0)
        out.append({"class": name, "path": p.replace(os.sep, "/"), "files": files, "bytes": size, "what": why})
    db_files, db_bytes = 0, 0
    for suffix in ("", "-wal", "-shm"):
        f = home.db_path + suffix
        if os.path.isfile(f):
            db_files += 1
            db_bytes += os.path.getsize(f)
    out.append({"class": "database", "path": home.db_path.replace(os.sep, "/"), "files": db_files, "bytes": db_bytes,
                "what": "SQLite database: sources, revisions, units, statements, chunks, commits, proposals, audit"})
    return out


# ------------------------------------------------------------------------------------------------ what hiding leaves behind (T-D3, Gate 1 H2)
# Expected presence of a unique test string AFTER a source was hidden (level a or b) and the next commit was published (it prunes older versions, erases
# the derived text from the database, blanks the proposals that name the hidden thing, removes backups and stale working folders, and compacts the file).
#   present          the class keeps it ON PURPOSE (Gate 1 decision 4: the original file stays)
#   absent           must not be found
#   copy_of_database a pre-migration backup is a copy of the whole database, so it holds whatever the database held; no assertion (not used any more:
#                    backups are removed once anything is hidden)
FILE_EXPECTATION = {"raw_blobs": "present", "index_segments": "absent", "staging": "absent", "backups": "absent",
                    "logs": "absent", "vector_cache": "absent", "opened_copies": "absent", "embedder_state": "absent",
                    "ask_answers": "absent", "ask_tickets": "absent"}
DB_EXPECTATION = {"db_derived_text": "absent", "db_proposals": "absent", "db_audit": "absent", "db_file_bytes": "absent"}


def scan(home, conn, needle):
    """Where does `needle` occur? {class name: bool}. File classes are searched byte by byte; database classes through their tables."""
    data = needle.encode("utf-8")
    found = {}
    for name, sub, _ in CLASSES:
        hit, p = False, home.sub(sub)
        for d, _, names in os.walk(p) if os.path.isdir(p) else []:
            for n in names:
                try:
                    with open(os.path.join(d, n), "rb") as f:
                        hit = hit or data in f.read()
                except OSError:
                    pass
        found[name] = hit
    q = lambda sql: conn.execute(sql, (needle,)).fetchone() is not None          # noqa: E731
    found["db_derived_text"] = (q("SELECT 1 FROM chunks WHERE instr(text, ?) > 0") or q("SELECT 1 FROM statements WHERE instr(value_json, ?) > 0"))
    found["db_proposals"] = q("SELECT 1 FROM proposals WHERE instr(diff_json, ?) > 0")
    found["db_audit"] = q("SELECT 1 FROM audit_events WHERE instr(ids_json, ?) > 0")
    raw = False                                                    # SQLite does not overwrite what an UPDATE replaces: look at the bytes of the file and its log
    for suffix in ("", "-wal", "-shm"):
        try:
            with open(home.db_path + suffix, "rb") as f:
                raw = raw or data in f.read()
        except OSError:
            pass
    found["db_file_bytes"] = raw
    return found


def expectations():
    out = dict(FILE_EXPECTATION)
    out.update(DB_EXPECTATION)
    return out


def check_after_hide(home, conn, needle):
    """[(class, expected, found)] for every class whose state differs from what hiding promises. Empty means the table holds."""
    found = scan(home, conn, needle)
    bad = []
    for name, want in expectations().items():
        if want == "copy_of_database":
            continue
        if (want == "present") != found[name]:
            bad.append((name, want, found[name]))
    return bad
