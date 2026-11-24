"""Diagnosis only (no tuning): why does kb's lexical search miss a few queries that A0's BM25 variant hits?
The two systems differ in (1) how pages are cut into chunks and (2) the BM25 formula (A0: k1=1.5 and idf = log(1 + (N-n+0.5)/(n+0.5));
SQLite FTS5: k1=1.2 and idf = log((N-n+0.5)/(n+0.5)) floored at 1e-6). This script scores the same queries in a 2x2 design, chunking x formula,
with pure-Python BM25 over token lists, plus a sanity cell that must reproduce each system's own saved result.
Nothing here changes a parameter, a frozen file or kb. Cells are DESCRIPTIVE: no cell is chosen for being best on the baseline sets.
Run: PYTHONUTF8=1 python tools/s1_diag_decompose.py <parent that holds wiki_home>"""
import collections
import glob
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, "C:/Users/roy/roy_km/scripts")
import wiki_embed as we  # noqa: E402
from kb import db, ftsindex  # noqa: E402
from kb import tokenize as tk  # noqa: E402
from kb.home import Home  # noqa: E402
from kb.search import Searcher  # noqa: E402

FORMULAS = {"A0 (k1=1.5, idf=log(1+x))": (1.5, 0.75, "log1p"), "FTS5 (k1=1.2, idf=log(x), floor)": (1.2, 0.75, "fts5")}


def build_index(chunks):
    """chunks: list of (page, token list)."""
    inv = collections.defaultdict(list)
    for i, (_, toks) in enumerate(chunks):
        for t, c in collections.Counter(toks).items():
            inv[t].append((i, c))
    dl = [len(t) for _, t in chunks]
    return chunks, inv, dl, sum(dl) / len(chunks)


def page_ranks(index, query_tokens, formula):
    """Pages ranked by their best chunk score (positive scores only)."""
    chunks, inv, dl, avg = index
    k1, b, idf_kind = formula
    n = len(chunks)
    score = collections.defaultdict(float)
    for t in set(query_tokens):
        post = inv.get(t)
        if not post:
            continue
        x = (n - len(post) + 0.5) / (len(post) + 0.5)
        idf = math.log(1 + x) if idf_kind == "log1p" else (math.log(x) if math.log(x) > 0 else 1e-6)
        for i, c in post:
            score[i] += idf * c * (k1 + 1) / (c + k1 * (1 - b + b * dl[i] / avg))
    best = {}
    for i, s in score.items():
        if s > 0 and (chunks[i][0] not in best or s > best[chunks[i][0]]):
            best[chunks[i][0]] = s
    return sorted(best, key=lambda p: -best[p])


def main():
    parent = sys.argv[1]
    h = Home(os.path.join(parent, "wiki_home"))
    conn = db.connect(h.db_path, readonly=True)
    s = Searcher(h, conn)
    _, _, fts = s._active()
    kb_chunks = [(os.path.splitext(s._source_info(src)[1])[0], tokens.split()) for _, _, src, _, tokens in ftsindex.read_map(fts)]
    a0_chunks = []
    for f in sorted(we.discover_pages()):
        name = os.path.splitext(os.path.basename(f))[0]
        for cname, text in we.chunk_page(name, open(f, encoding="utf-8").read()):
            a0_chunks.append((cname.split("#")[0], we.bm25_tokens(text)))
    print("chunks: kb", len(kb_chunks), " A0", len(a0_chunks), " mean tokens/chunk: kb %.1f A0 %.1f" % (
        sum(len(t) for _, t in kb_chunks) / len(kb_chunks), sum(len(t) for _, t in a0_chunks) / len(a0_chunks)))
    kb_idx, a0_idx = build_index(kb_chunks), build_index(a0_chunks)
    cells = {("kb chunks", f): kb_idx for f in FORMULAS} | {("A0 chunks", f): a0_idx for f in FORMULAS}
    out = {"cells": {}, "queries": {}}
    for name in ("real", "blind1", "blind2"):
        rows = [r for r in json.load(open(os.path.join(ROOT, "s1", "a0", name + ".json"), encoding="utf-8"))["results"]
                if r["gold"] and not r["gold_missing_from_index"]]
        for (cname, fname), chunks in cells.items():
            hits, ranks = 0, {}
            for r in rows:
                pages = page_ranks(chunks, tk.tokens(r["query"]), FORMULAS[fname])
                gold = set(r["gold"])
                rk = next((i + 1 for i, p in enumerate(pages) if p in gold), None)
                hits += rk is not None and rk <= 5
                ranks[r["id"]] = rk
            out["cells"].setdefault(f"{cname} | {fname}", {})[name] = {"hits": hits, "n": len(rows)}
            out["queries"].setdefault(f"{cname} | {fname}", {})[name] = ranks
        a0_saved = sum(1 for r in rows if any(g in r["bm25"][:5] for g in r["gold"]))
        out["cells"].setdefault("A0 saved result", {})[name] = {"hits": a0_saved, "n": len(rows)}
    for label, v in out["cells"].items():
        print(f"{label:55s}", "  ".join(f"{n}: {v[n]['hits']}/{v[n]['n']}" for n in v))
    ids = {"real": ["Q1-5"], "blind1": ["D3"], "blind2": ["A1", "C4"]}
    print("\ngold-page rank (None = not retrieved) for the queries kb missed and A0-BM25 hit:")
    for label, per in out["queries"].items():
        print(f"{label:55s}", {q: per[n][q] for n, qs in ids.items() for q in qs})
    json.dump(out, open(os.path.join(ROOT, "docs", "s1_diag_decompose.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
