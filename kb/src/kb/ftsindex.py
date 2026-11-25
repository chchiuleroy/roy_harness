"""The lexical index artifact of one commit: a standalone SQLite file with an FTS5 table of pre-tokenised chunks (see tokenize.py)
and a map from rows back to chunk ids, sources and text hashes. The file is written once into a staging folder and never modified."""
import sqlite3

from . import tokenize as tk

INDEX_KIND = "fts5"


def token_string(text):
    return " ".join(tk.tokens(text))


def build(path, rows):
    """rows: iterable of (chunk_id, source_id, text, text_sha256). Returns the number of rows written."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("CREATE VIRTUAL TABLE fts USING fts5(tokens, tokenize='unicode61 remove_diacritics 0')")
        conn.execute("CREATE TABLE chunk_map(ordinal INTEGER PRIMARY KEY, chunk_id TEXT NOT NULL, source_id TEXT NOT NULL, "
                     "text_sha256 TEXT NOT NULL)")
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('tokenizer', ?)", (tk.TOKENIZER_ID,))
        n = 0
        conn.execute("BEGIN")
        for n, (chunk_id, source_id, text, sha) in enumerate(rows, 1):
            conn.execute("INSERT INTO fts(rowid, tokens) VALUES (?,?)", (n, token_string(text)))
            conn.execute("INSERT INTO chunk_map VALUES (?,?,?,?)", (n, chunk_id, source_id, sha))
        conn.execute("COMMIT")
        return n
    finally:
        conn.close()


def open_readonly(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def read_map(path):
    """All (ordinal, chunk_id, source_id, text_sha256, tokens) rows: what ghost checking compares against the expected content."""
    conn = open_readonly(path)
    try:
        return conn.execute("SELECT m.ordinal, m.chunk_id, m.source_id, m.text_sha256, f.tokens FROM chunk_map m "
                            "JOIN fts f ON f.rowid = m.ordinal ORDER BY m.ordinal").fetchall()
    finally:
        conn.close()


def search(path, query, limit=20):
    """Chunk-level BM25 search. Returns [(chunk_id, source_id, score)], best first (higher score is better)."""
    toks = sorted(set(tk.tokens(query)))
    if not toks:
        return []
    match = " OR ".join('"' + t + '"' for t in toks)
    conn = open_readonly(path)
    try:
        rows = conn.execute("SELECT m.chunk_id, m.source_id, -bm25(fts) AS score FROM fts JOIN chunk_map m ON m.ordinal = fts.rowid "
                            "WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?", (match, limit)).fetchall()
    finally:
        conn.close()
    return [(c, s, float(sc)) for c, s, sc in rows]
