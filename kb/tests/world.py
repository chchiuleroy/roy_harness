"""A small knowledge base built through the real intake and pipeline, shared by the commit, search and CLI tests."""
import os

from kb import cas, colls, commit, db, ftsindex, inbox, pipeline
from kb.home import Home

from .support import DbTestCase, tmpdir


class World(DbTestCase):
    def setUp(self):
        super().setUp()
        self.d = tmpdir(self)
        self.home = Home(os.path.join(self.d, "h"))
        self.home.init()
        self.conn = db.connect(self.home.db_path)
        self.cid = colls.create(self.conn, "docs")
        self.store = cas.BlobStore(self.home.sub("blobs"))
        self.intake = inbox.Intake(self.conn, self.store, self.cid, settle_s=0)
        self.src = os.path.join(self.d, "src")
        os.makedirs(self.src)

    def add(self, name, text, collection=None):
        p = os.path.join(self.src, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(text.encode("utf-8") if isinstance(text, str) else text)
        intake = self.intake if collection is None else inbox.Intake(self.conn, self.store, collection, settle_s=0)
        return intake.add_file(p)

    def build(self, **kw):
        pipeline.process_pending(self.conn, self.store)
        return commit.build(self.home, self.conn, **kw)

    def one(self, sql, *a):
        return self.conn.execute(sql, a).fetchone()

    def states(self):
        return {c: s for c, s in self.conn.execute("SELECT commit_id, state FROM commits")}

    def active_fts(self):
        return commit.active_artifacts(self.home, self.conn)[2]

    def search(self, q, k=10):
        return ftsindex.search(self.active_fts(), q, k)

    def chunk_text(self, chunk_id):
        return self.one("SELECT text FROM chunks WHERE chunk_id=?", chunk_id)[0]

    def hit_sources(self, q):
        return {s for _, s, _ in self.search(q)}

    # ---- stage S2 helpers
    def process(self):
        return pipeline.process_pending(self.conn, self.store)

    def uvs(self, source_id):
        """Unit versions of the source's current conversion, in document order."""
        conv = commit.current_conversion(self.conn, source_id)
        return [r[0] for r in self.conn.execute("SELECT unit_version_id FROM conversion_units WHERE conv_id=? ORDER BY ordinal", (conv[0],))]

    def decide(self, kind, diff, by="roy", **approve_kw):
        """Create, approve and apply a proposal in one go (the person asking is the person deciding). Returns the proposal id."""
        from kb import review
        pid = review.create(self.conn, kind, diff, by)
        review.approve(self.conn, pid, by, **approve_kw)
        review.apply(self.conn, pid)
        return pid

    def displayed(self, subject_id, key):
        """(value, origin) of the statement that the ACTIVE commit shows for (subject, key), or None."""
        row = self.conn.execute(
            "SELECT s.value_json, s.origin FROM statements s JOIN commit_statements cs ON cs.statement_id = s.statement_id "
            "WHERE cs.commit_id = ? AND s.subject_id = ? AND s.key = ? AND cs.role = 'display'", (commit.active_commit_id(self.conn), subject_id, key)).fetchone()
        return (__import__("json").loads(row[0]), row[1]) if row else None

    def table_hash(self, *tables):
        import hashlib
        h = hashlib.sha256()
        for t in tables:
            for row in self.conn.execute(f'SELECT * FROM "{t}" ORDER BY 1'):
                h.update(repr(row).encode())
        return h.hexdigest()

    def seed_docs(self):
        self.a = self.add("alpha.md", "# Alpha\n\nThe falcon flies over the harbor at dawn.\n\n## Part two\n\nSecond section about kelp forests.\n")
        self.b = self.add("beta.txt", "資料科學與統計學的筆記。\n\n貝氏推論與先驗分配。")
        self.c = self.add("gamma.csv", "name,city\nAnn,Taipei\nBob,Kaohsiung\n")
