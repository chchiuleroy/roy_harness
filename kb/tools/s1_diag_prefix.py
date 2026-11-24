"""Diagnosis only (no tuning): A0's BM25 scores the stored index, which keeps only the first 400 characters of every chunk (wiki_embed.py:
`"text": ctext[:400]`), while kb scores the whole chunk. Does that difference explain the kb-versus-A0 gaps?
Cells (all over the current wiki, page score = best chunk, positive scores only, Success@5 on the valid queries of the three baseline sets):
  A0 stored index  x A0 formula      -> must reproduce A0's saved result (sanity)
  A0 stored index  x FTS5 formula
  kb chunks (full) x FTS5 formula    -> must reproduce kb's evaluation (sanity)
  kb chunks (full) x A0 formula
  kb chunks cut to their first 400 characters x FTS5 formula / A0 formula
The cut is the experiment: it gives kb the same visible text as A0, keeping kb's own chunk boundaries. Nothing is tuned and no cell is chosen.
Run: PYTHONUTF8=1 python tools/s1_diag_prefix.py <parent that holds wiki_home>"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s1_diag_decompose as dd  # noqa: E402

sys.path.insert(0, "C:/Users/roy/roy_km/scripts")
import eval_bm25_shadow as es  # noqa: E402

ROOT, tk, ftsindex = dd.ROOT, dd.tk, dd.ftsindex
A0F, FTSF = dd.FORMULAS["A0 (k1=1.5, idf=log(1+x))"], dd.FORMULAS["FTS5 (k1=1.2, idf=log(x), floor)"]
CUT = 400


def main():
    h = dd.Home(os.path.join(sys.argv[1], "wiki_home"))
    conn = dd.db.connect(h.db_path, readonly=True)
    s = dd.Searcher(h, conn)
    _, _, fts = s._active()
    text_of = dict(conn.execute("SELECT chunk_id, text FROM chunks"))
    rows_map = ftsindex.read_map(fts)
    stem = {}
    kb_full, kb_cut = [], []
    for _, chunk_id, src, _, _ in rows_map:
        if src not in stem:
            stem[src] = os.path.splitext(s._source_info(src)[1])[0]
        t = text_of[chunk_id]
        kb_full.append((stem[src], tk.tokens(t)))
        kb_cut.append((stem[src], tk.tokens(t[:CUT])))
    ids, texts, _ = es.load_chunks()
    a0_stored = [(es.page_of(i), es.tok(t)) for i, t in zip(ids, texts)]
    cells = {
        "A0 stored index (first 400 chars) x A0 formula": (dd.build_index(a0_stored), A0F),
        "A0 stored index (first 400 chars) x FTS5 formula": (dd.build_index(a0_stored), FTSF),
        "kb chunks, full text x FTS5 formula": (dd.build_index(kb_full), FTSF),
        "kb chunks, full text x A0 formula": (dd.build_index(kb_full), A0F),
        "kb chunks cut to first 400 chars x FTS5 formula": (dd.build_index(kb_cut), FTSF),
        "kb chunks cut to first 400 chars x A0 formula": (dd.build_index(kb_cut), A0F),
    }
    mean = lambda c: sum(len(t) for _, t in c) / len(c)                           # noqa: E731
    print("mean tokens per chunk: A0 stored %.1f, kb full %.1f, kb cut %.1f" % (mean(a0_stored), mean(kb_full), mean(kb_cut)))
    focus = {"real": ["Q1-5"], "blind1": ["D3"], "blind2": ["A1", "C4"]}
    out = {}
    for name in ("real", "blind1", "blind2"):
        rows = [r for r in json.load(open(os.path.join(ROOT, "s1", "a0", name + ".json"), encoding="utf-8"))["results"]
                if r["gold"] and not r["gold_missing_from_index"]]
        saved = sum(1 for r in rows if set(r["gold"]) & set(r["bm25"][:5]))
        out.setdefault("A0 saved result", {})[name] = f"{saved}/{len(rows)}"
        for label, (idx, f) in cells.items():
            hits, ranks = 0, {}
            for r in rows:
                pages = dd.page_ranks(idx, tk.tokens(r["query"]), f)
                rk = next((i + 1 for i, p in enumerate(pages) if p in set(r["gold"])), None)
                hits += rk is not None and rk <= 5
                if r["id"] in focus[name]:
                    ranks[r["id"]] = rk
            out.setdefault(label, {})[name] = f"{hits}/{len(rows)}"
            out.setdefault("focus ranks | " + label, {}).update(ranks)
    for label, v in out.items():
        print(f"{label:58s}", v)
    json.dump(out, open(os.path.join(ROOT, "docs", "s1_diag_prefix.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
