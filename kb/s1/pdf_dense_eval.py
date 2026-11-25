"""S-1: dense and hybrid baselines on the PDF passage tasks, GAP GROUP ONLY (acceptance results are withheld until S9:
set KB_S9=1 to include them). Page-level: a page's score is its best chunk. Systems: bm25 (page docs), dense
(Qwen3-Embedding resident), hybrid (RRF k=60 over the two page rankings). Also measures the memory of a small
resident-server config (-np 1 -c 4096) and query latency with it.
Run after embed_pdf_corpus.py: PYTHONUTF8=1 python pdf_dense_eval.py"""
import collections
import json
import math
import os
import pickle
import re
import subprocess
import sys
import time
import urllib.request

import numpy as np

sys.path.insert(0, "C:/Users/roy/roy_km/scripts")
import wiki_embed as we

HERE = os.path.dirname(os.path.abspath(__file__)) + "/"
SERVER = "C:/wcpp/llama.cpp/build/bin/Release/llama-server.exe"
MODEL = "C:/wcpp/models/Qwen3-Embedding-0.6B-Q8_0.gguf"
PORT = 8794
NL = chr(10)
LATIN, CJK = re.compile(r"[a-z0-9]+"), re.compile(r"[\u4e00-\u9fff]+")
GROUPS = {"gap", "acceptance"} if os.environ.get("KB_S9") == "1" else {"gap"}


def tok(s):
    s = s.lower()
    out = [t for t in LATIN.findall(s) if len(t) >= 2 or t.isdigit()]
    for run in CJK.findall(s):
        out += [run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)]
    return out


def rss_mb(pid):
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True)
    cell = r.stdout.strip().split('","')[-1].strip('"').replace(" K", "").replace(",", "")
    return round(int(cell) / 1024) if cell.isdigit() else None


def main():
    tasks = [t for t in json.load(open(HERE + "tasks/pdf_tasks.json", encoding="utf-8")) if t["group"] in GROUPS]
    meta = json.load(open(HERE + "results/pdf_chunks.json", encoding="utf-8"))
    shards = sorted(f for f in os.listdir(HERE + "results/pdf_vecs") if f.endswith(".npy") and "tmp" not in f)
    C = np.concatenate([np.load(HERE + "results/pdf_vecs/" + f) for f in shards]).astype(np.float32)
    assert len(C) == len(meta), (len(C), len(meta))
    page_key = [(m["pdf"], m["page"]) for m in meta]
    pages = sorted(set(page_key))
    pidx = {p: i for i, p in enumerate(pages)}
    chunk_page = np.array([pidx[p] for p in page_key])
    page_chunks = np.bincount(chunk_page, minlength=len(pages))

    # BM25 at page level over the same text the chunks came from
    # ponytail: the pickle is this project's own cache (pdf_bm25_baseline.py)
    texts = pickle.load(open(HERE + "results/pdf_pages.pkl", "rb"))
    tdict = {(f, p): t for f, p, t in texts}
    docs = [collections.Counter(tok(tdict[p])) for p in pages]
    lens = np.array([sum(c.values()) for c in docs], dtype=np.float64)
    df = collections.Counter()
    for c in docs:
        df.update(c.keys())
    N = len(docs)
    idf = {t: math.log(1 + (N - n + 0.5) / (n + 0.5)) for t, n in df.items()}
    inv = collections.defaultdict(list)
    for j, c in enumerate(docs):
        for t, n in c.items():
            inv[t].append((j, n))

    def bm25(q):
        sc = np.zeros(N)
        for t in set(tok(q)):
            for j, n in inv.get(t, []):
                sc[j] += idf[t] * n * 2.5 / (n + 1.5 * (0.25 + 0.75 * lens[j] / lens.mean()))
        return sc

    proc = subprocess.Popen([SERVER, "-m", MODEL, "--embeddings", "--pooling", "last", "-ngl", "99", "-np", "1", "-c", "4096",
                             "-b", "4096", "-ub", "4096", "--host", "127.0.0.1", "--port", str(PORT)],
                            stdout=subprocess.DEVNULL, stderr=open(HERE + "server4.log", "wb"))
    mem = {}
    try:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 120:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.3)

        def embed(q):
            body = {"input": [f"Instruct: {we.QUERY_INSTRUCTION}{NL}Query:{q}".replace(NL, " ")], "model": "x"}
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/embeddings", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            v = np.array(json.loads(urllib.request.urlopen(req, timeout=60).read())["data"][0]["embedding"], dtype=np.float32)
            return v / np.linalg.norm(v)

        embed("warmup")
        lat, rows = [], []
        for t in tasks:
            s = time.perf_counter()
            qv = embed(t["query"])
            lat.append((time.perf_counter() - s) * 1000)
            sims = C @ qv
            dense = np.full(N, -1e9)
            np.maximum.at(dense, chunk_page, sims)
            sparse = bm25(t["query"])

            def top(sc, k=10):
                return [pages[j] for j in np.argsort(-sc)[:k]]

            # only pages with a positive BM25 score are results; zero-hit queries return an empty list, and the
            # RRF below never receives filler pages (小o review 2026-11-16: the earlier version padded the list)
            d, b = top(dense), [pages[j] for j in np.argsort(-sparse)[:10] if sparse[j] > 0]
            rr = collections.defaultdict(float)
            for lst in (d, b):
                for r, p in enumerate(lst):
                    rr[p] += 1 / (60 + r + 1)
            h = [p for p, _ in sorted(rr.items(), key=lambda x: -x[1])][:10]
            g = (t["gold"]["pdf"], t["gold"]["page"])
            rec = {"id": t["id"], "group": t["group"], "category": t["category"],
                   "gold_page_chunks": int(page_chunks[pidx[(t["gold"]["pdf"], t["gold"]["page"])]]),
                   "dense_top1_page_chunks": int(page_chunks[pidx[d[0]]])}
            for name, lst in (("bm25", b), ("dense", d), ("hybrid", h)):
                rec[name] = {"L1": any(p[0] == g[0] for p in lst[:5]), "L2": g in lst[:5], "top1": lst[:1] == [g]}
            rows.append(rec)
        mem["server_rss_mb_after_queries"] = rss_mb(proc.pid)
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)

    def agg(rs, sysname):
        return {"n": len(rs), "L1@5": sum(r[sysname]["L1"] for r in rs), "L2@5": sum(r[sysname]["L2"] for r in rs),
                "L2@1": sum(r[sysname]["top1"] for r in rs)}

    out = {"groups_evaluated": sorted(GROUPS), "n_chunks": len(C), "n_pages": N,
           "small_server": {"flags": "-np 1 -c 4096", "query_ms_median": round(float(np.median(lat)), 1),
                            "query_ms_p95": round(float(np.percentile(lat, 95)), 1), **mem}}
    out["overall"] = {s: agg(rows, s) for s in ("bm25", "dense", "hybrid")}
    out["by_category"] = {c: {s: agg([r for r in rows if r["category"] == c], s) for s in ("bm25", "dense", "hybrid")}
                          for c in sorted({r["category"] for r in rows})}
    # page-length diagnostic (max-chunk aggregation favours pages with more chunks): L2 hit rate by gold page chunk count
    med = float(np.median([r["gold_page_chunks"] for r in rows]))
    out["page_length_diagnostic"] = {"median_gold_page_chunks": med,
        "dense_L2_short_gold_pages": agg([r for r in rows if r["gold_page_chunks"] <= med], "dense"),
        "dense_L2_long_gold_pages": agg([r for r in rows if r["gold_page_chunks"] > med], "dense"),
        "median_dense_top1_page_chunks": float(np.median([r["dense_top1_page_chunks"] for r in rows])),
        "all_pages_median_chunks": float(np.median(page_chunks[page_chunks > 0]))}
    out["per_task"] = rows
    json.dump(out, open(HERE + "results/pdf_dense_eval_gap.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "per_task"}, ensure_ascii=False))


main()
