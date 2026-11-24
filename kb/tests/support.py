"""Shared test helpers (no test cases here)."""
import hashlib
import os
import subprocess
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def tmpdir(testcase):
    d = tempfile.mkdtemp(prefix="kb_test_")
    import shutil
    testcase.addCleanup(shutil.rmtree, d, True)
    return d


class DbTestCase(__import__("unittest").TestCase):
    """Closes every sqlite connection a test opens (Windows cannot delete a database that is still open)."""

    def setUp(self):
        from kb import db
        self._conns = []
        original = db.connect

        def tracking(*a, **k):
            c = original(*a, **k)
            self._conns.append(c)
            return c
        db.connect = tracking
        self.addCleanup(setattr, db, "connect", original)
        self.addCleanup(lambda: [c.close() for c in self._conns])


def approved_proposal(conn, proposal_id, kind="delete", by="roy", override=0, diff="{}"):
    """A proposal that went through the legal path (pending, then an approval row, then 'approved'), written directly: for tests of the layers
    below review. Does nothing if the proposal already exists."""
    if conn.execute("SELECT 1 FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone():
        return
    conn.execute("INSERT INTO proposals(proposal_id,kind,diff_json,requested_by,state,created_at) VALUES (?,?,?,?, 'pending', 't')",
                 (proposal_id, kind, diff, by))
    conn.execute("INSERT INTO approvals(approval_id,proposal_id,approved_by,at,override_protected) VALUES (?,?,?, 't', ?)",
                 ("ap_" + proposal_id[3:], proposal_id, by, override))
    conn.execute("UPDATE proposals SET state='approved' WHERE proposal_id=?", (proposal_id,))


def run_py(code, env_extra=None, check=False):
    env = dict(os.environ, PYTHONPATH=SRC, PYTHONUTF8="1")
    env.pop("KB_FAULT", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=check)


def db_fingerprint(conn):
    """Hash of schema plus every row of every table, plus user_version: 'the database did not change'."""
    h = hashlib.sha256()
    h.update(str(conn.execute("PRAGMA user_version").fetchone()[0]).encode())
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        h.update(f"{name}|{sql}".encode())
        if conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()[0] == "table":
            for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall():
                h.update(repr(row).encode())
    return h.hexdigest()


class FakeEmbedder:
    """A deterministic stand-in for the embedding backend: a hashed bag of words over `dim` dimensions, unit length. `concepts` maps words to a
    shared concept word, which is how a test makes two different words 'semantically close' (a stand-in for synonyms). Records its calls."""

    def __init__(self, dim=64, concepts=None, model="fake-model", instruction="find passages"):
        from kb import embedder
        self.dim, self.concepts, self.instruction, self.model = dim, dict(concepts or {}), instruction, model
        self._identity = embedder.Identity(model, hashlib.sha256(model.encode()).hexdigest(), dim, "last", instruction)
        self.document_calls, self.query_calls = [], []

    @property
    def identity(self):
        return self._identity

    def _vec(self, text):
        import re

        import numpy as np
        v = np.zeros(self.dim, dtype=np.float32)
        for tok in re.findall(r"\w+", text.lower()):
            tok = self.concepts.get(tok, tok)
            h = hashlib.sha256(tok.encode()).digest()
            v[int.from_bytes(h[:4], "big") % self.dim] += 1.0 if h[4] & 1 else -1.0
        n = float(np.linalg.norm(v))
        if n == 0:
            v[0] = 1.0
            n = 1.0
        return v / n

    def embed_documents(self, texts):
        import numpy as np
        self.document_calls.append(list(texts))
        return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim), dtype=np.float32)

    def embed_query(self, text):
        self.query_calls.append(text)
        return self._vec(text)
