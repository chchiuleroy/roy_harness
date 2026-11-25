"""Diagnosis only: why does query A1 of blind2 rank the gold page 2nd in A0 but 5th to 9th in every kb cell? Prints the query tokens, the pages
ranked above the gold page in kb and in A0 (with the matched terms of each page's best chunk), and the gold page's best chunk in each system.
Run: PYTHONUTF8=1 python tools/s1_diag_a1.py <parent that holds wiki_home> [query id in blind2, default A1]"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s1_diag_decompose as dd  # noqa: E402

sys.path.insert(0, "C:/Users/roy/roy_km/scripts")
import eval_bm25_shadow as es  # noqa: E402

qid = sys.argv[2] if len(sys.argv) > 2 else "A1"
row = next(r for r in json.load(open(os.path.join(dd.ROOT, "s1", "a0", "blind2.json"), encoding="utf-8"))["results"] if r["id"] == qid)
gold = row["gold"][0]
qt = sorted(set(dd.tk.tokens(row["query"])))
print(row["query"], "| tokens:", qt, "| gold:", gold)

h = dd.Home(os.path.join(sys.argv[1], "wiki_home"))
conn = dd.db.connect(h.db_path, readonly=True)
s = dd.Searcher(h, conn)
_, _, fts = s._active()
text_of = dict(conn.execute("SELECT chunk_id, text FROM chunks"))
kb = []
for _, cid, src, _, _ in dd.ftsindex.read_map(fts):
    kb.append((os.path.splitext(s._source_info(src)[1])[0], text_of[cid]))
ids, texts, _ = es.load_chunks()
a0 = [(es.page_of(i), t) for i, t in zip(ids, texts)]


def show(label, chunks, tok, formula):
    idx = dd.build_index([(p, tok(t)) for p, t in chunks])
    pages = dd.page_ranks(idx, tok(row["query"]), formula)
    print("\n==", label, "| gold rank:", next((i + 1 for i, p in enumerate(pages) if p == gold), None), "| #chunks", len(chunks))
    for p in pages[:8]:
        best = max((c for c in chunks if c[0] == p), key=lambda c: sum(tok(c[1]).count(t) for t in qt))
        hit = [t for t in qt if t in set(tok(best[1]))]
        print(("  GOLD " if p == gold else "       "), p, "| terms in its best chunk:", hit, "| chunk len", len(best[1]))
    gchunks = [c for c in chunks if c[0] == gold]
    best = max(gchunks, key=lambda c: sum(tok(c[1]).count(t) for t in qt))
    print("  gold page has", len(gchunks), "chunks; best:", [t for t in qt if t in set(tok(best[1]))], "|", repr(best[1][:230]))


show("A0 stored index", a0, es.tok, dd.FORMULAS["A0 (k1=1.5, idf=log(1+x))"])
show("kb chunks (full)", kb, dd.tk.tokens, dd.FORMULAS["FTS5 (k1=1.2, idf=log(x), floor)"])
