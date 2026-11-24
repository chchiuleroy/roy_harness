"""Diagnosis only (no tuning): on the baseline sets, which queries does kb's lexical search miss that A0's BM25 variant hit, and vice versa?
For each difference: the gold page's rank in kb (beyond top 5 too) and in A0, and the query category. Reads the kb home that tools/s1_eval.py left
in <parent>/wiki_home and the per-query A0 outputs saved in s1/a0/*.json. Nothing here changes any parameter or any frozen file.
Run: PYTHONUTF8=1 python tools/s1_diag.py <parent>"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from kb import db  # noqa: E402
from kb.home import Home  # noqa: E402
from kb.search import Searcher  # noqa: E402

h = Home(os.path.join(sys.argv[1], "wiki_home"))
conn = db.connect(h.db_path, readonly=True)
s = Searcher(h, conn)
out = {}
for name in ("real", "blind1", "blind2"):
    rows = json.load(open(os.path.join(ROOT, "s1", "a0", name + ".json"), encoding="utf-8"))["results"]
    diffs = []
    for r in rows:
        if not r["gold"] or r["gold_missing_from_index"]:
            continue
        gold = set(r["gold"])
        kb_pages = [os.path.splitext(x.name)[0] for x in s.sources(r["query"], 30)]
        a0_pages = r["bm25"]
        kb_hit = any(g in kb_pages[:5] for g in gold)
        a0_hit = any(g in a0_pages[:5] for g in gold)
        if kb_hit != a0_hit:
            rank = lambda lst: next((i + 1 for i, p in enumerate(lst) if p in gold), None)       # noqa: E731
            diffs.append({"id": r["id"], "category": r["category"], "query": r["query"], "kb_hit": kb_hit, "a0_bm25_hit": a0_hit,
                          "kb_rank": rank(kb_pages), "a0_rank": rank(a0_pages)})
    out[name] = {"kb_only": [d for d in diffs if d["kb_hit"]], "a0_only": [d for d in diffs if d["a0_bm25_hit"]]}
    print(name, "kb-only hits:", len(out[name]["kb_only"]), " a0-bm25-only hits:", len(out[name]["a0_only"]))
    for d in out[name]["a0_only"]:
        print("   A0 hit, kb missed:", d["id"], d["category"], "kb_rank", d["kb_rank"], "a0_rank", d["a0_rank"], "|", d["query"][:50])
json.dump(out, open(os.path.join(ROOT, "docs", "s1_diag.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
