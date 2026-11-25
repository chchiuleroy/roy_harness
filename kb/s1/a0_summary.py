"""Summarise the A0 baseline runs (existing wiki shadow-eval outputs in a0/*.json) into results/a0_summary.json:
Success@5 per system per set, valid queries only (gold present in index). The strongest A0 variant per set is the
comparator for the non-inferiority rule (so A is not compared against a strawman)."""
import json

HERE = "C:/Users/roy/kb/s1/"
SYS = ["vector", "bm25", "hybrid_rrf", "hybrid_pos", "hybrid_page", "bm25_title", "hybrid_title", "hybrid_title_pos", "hybrid_title_page"]
out = {}
for name in ("real", "blind1", "blind2"):
    d = json.load(open(HERE + f"a0/{name}.json", encoding="utf-8"))
    rows = [r for r in d["results"] if r["gold"] and not r["gold_missing_from_index"]]
    res = {s: sum(int(any(g in r[s][:5] for g in r["gold"])) for r in rows) for s in SYS}
    best = max(res, key=lambda s: res[s])
    out[name] = {"n_valid": len(rows), "n_total": len(d["results"]), "success_at_5": res, "best_variant": best,
                 "best_score": res[best], "production_default_vector": res["vector"],
                 "index": {"chunks": d["n_chunks"], "pages": d["n_pages"]}}
json.dump(out, open(HERE + "results/a0_summary.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
for k, v in out.items():
    print(k, v["n_valid"], "vector", v["production_default_vector"], "best", v["best_variant"], v["best_score"])
