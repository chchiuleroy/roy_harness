"""S-1 baseline on the PDF passage tasks: page-level BM25 (k1=1.5, b=0.75, not tuned; CJK bigram + latin tokens,
same tokenizer as the existing wiki shadow eval) over every page of roy_km/_raw PDFs. This is a task-sanity and
floor measurement, NOT a candidate design. Levels (Gate 1 O): L1 = right PDF in top-5, L2 = right page in top-5.
Run: PYTHONUTF8=1 python pdf_bm25_baseline.py"""
import collections
import json
import math
import os
import pickle
import re
import time

import fitz
import numpy as np

HERE = "C:/Users/roy/kb/s1/"
RAW = "C:/Users/roy/roy_km/_raw"
CACHE = HERE + "results/pdf_pages.pkl"
LATIN = re.compile(r"[a-z0-9]+")
CJK = re.compile(r"[\u4e00-\u9fff]+")


def tok(s):
    s = s.lower()
    out = [t for t in LATIN.findall(s) if len(t) >= 2 or t.isdigit()]
    for run in CJK.findall(s):
        out += [run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)]
    return out


def load_pages():
    if os.path.exists(CACHE):
        return pickle.load(open(CACHE, "rb"))
    pages = []
    for f in sorted(os.listdir(RAW)):
        if not f.lower().endswith(".pdf"):
            continue
        doc = fitz.open(os.path.join(RAW, f))
        for i, pg in enumerate(doc):
            pages.append((f, i + 1, re.sub(r"\s+", " ", pg.get_text()).strip()))
        doc.close()
    pickle.dump(pages, open(CACHE, "wb"))
    return pages


def main():
    pages = load_pages()
    docs = [collections.Counter(tok(t)) for _, _, t in pages]
    lens = np.array([sum(c.values()) for c in docs], dtype=np.float64)
    avg = lens.mean()
    df = collections.Counter()
    for c in docs:
        df.update(c.keys())
    N = len(docs)
    idf = {t: math.log(1 + (N - n + 0.5) / (n + 0.5)) for t, n in df.items()}
    inv = collections.defaultdict(list)
    for j, c in enumerate(docs):
        for t, n in c.items():
            inv[t].append((j, n))
    k1, b = 1.5, 0.75

    def search(q, k=10):
        sc = np.zeros(N)
        for t in set(tok(q)):
            if t not in inv:
                continue
            w = idf[t]
            for j, n in inv[t]:
                sc[j] += w * n * (k1 + 1) / (n + k1 * (1 - b + b * lens[j] / avg))
        top = np.argsort(-sc)[:k]
        return [(pages[j][0], pages[j][1]) for j in top if sc[j] > 0]

    tasks = json.load(open(HERE + "tasks/pdf_tasks.json", encoding="utf-8"))
    rows, lat = [], []
    for t in tasks:
        s = time.perf_counter()
        top = search(t["query"])
        lat.append((time.perf_counter() - s) * 1000)
        g = (t["gold"]["pdf"], t["gold"]["page"])
        l1 = any(p[0] == g[0] for p in top[:5])
        l2 = g in top[:5]
        rank = next((i + 1 for i, p in enumerate(top) if p == g), None)
        rows.append({"id": t["id"], "group": t["group"], "category": t["category"], "L1": l1, "L2": l2, "rank": rank})

    def agg(rs):
        n = len(rs)
        return {"n": n, "L1_pdf_top5": sum(r["L1"] for r in rs), "L2_page_top5": sum(r["L2"] for r in rs),
                "L2_page_top1": sum(r["rank"] == 1 for r in rs)}

    out = {"n_pages_indexed": N, "bm25_query_ms_median": round(float(np.median(lat)), 1),
           "all": agg(rows)}
    for key in ("group", "category"):
        out["by_" + key] = {v: agg([r for r in rows if r[key] == v]) for v in sorted({r[key] for r in rows})}
    out["per_task"] = rows
    json.dump(out, open(HERE + "results/pdf_bm25_baseline.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "per_task"}, ensure_ascii=False))


main()
