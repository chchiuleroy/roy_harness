"""S3 step 1: diagnose the PDF page-location gap that S-1 left open (dense finds the file 28/30 but the page 18/30). GAP GROUP ONLY.
For every gap task it records where the gold page ranks in the dense page ranking and WHAT the pages ranked above it are:
  same file, next to the gold page (adjacent) | same file, further away | another file; and how much of their text is shared with the gold page
  (character 8-gram containment of the gold page in the other page), which says whether the gold is the only page that could answer.
Nothing here tunes anything; it only classifies the failures. Run: python tools/s3_diag_gap.py (needs the S-1 vectors in s1/results)."""
import json
import os
import pickle
import subprocess
import sys
import time
import urllib.request

import numpy as np

sys.path.insert(0, "C:/Users/roy/roy_km/scripts")
import wiki_embed as we

S1 = "C:/Users/roy/kb/s1/"
SERVER = "C:/wcpp/llama.cpp/build/bin/Release/llama-server.exe"
MODEL = "C:/wcpp/models/Qwen3-Embedding-0.6B-Q8_0.gguf"
PORT = 8795
NL = chr(10)


def grams(text, n=8):
    t = "".join(text.split())
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def main():
    tasks = [t for t in json.load(open(S1 + "tasks/pdf_tasks.json", encoding="utf-8")) if t["group"] == "gap"]
    assert tasks and all(t["group"] == "gap" for t in tasks)
    meta = json.load(open(S1 + "results/pdf_chunks.json", encoding="utf-8"))
    shards = sorted(f for f in os.listdir(S1 + "results/pdf_vecs") if f.endswith(".npy") and "tmp" not in f)
    C = np.concatenate([np.load(S1 + "results/pdf_vecs/" + f) for f in shards]).astype(np.float32)
    assert len(C) == len(meta)
    page_key = [(m["pdf"], m["page"]) for m in meta]
    pages = sorted(set(page_key))
    pidx = {p: i for i, p in enumerate(pages)}
    chunk_page = np.array([pidx[p] for p in page_key])
    # ponytail: the pickle is this project's own cache written by s1/pdf_bm25_baseline.py, not data from outside
    texts = {(f, p): t for f, p, t in pickle.load(open(S1 + "results/pdf_pages.pkl", "rb"))}

    proc = subprocess.Popen([SERVER, "-m", MODEL, "--embeddings", "--pooling", "last", "-ngl", "99", "-np", "1", "-c", "4096", "-b", "4096",
                             "-ub", "4096", "--host", "127.0.0.1", "--port", str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows = []
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
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/embeddings", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            v = np.array(json.loads(urllib.request.urlopen(req, timeout=60).read())["data"][0]["embedding"], dtype=np.float32)
            return v / np.linalg.norm(v)

        embed("warmup")
        for t in tasks:
            sims = C @ embed(t["query"])
            dense = np.full(len(pages), -1e9)
            np.maximum.at(dense, chunk_page, sims)
            order = np.argsort(-dense)
            g = (t["gold"]["pdf"], t["gold"]["page"])
            rank = int(np.where(order == pidx[g])[0][0]) + 1
            gold_grams = grams(texts[g])
            above = []
            for j in order[:min(rank - 1, 5)]:
                p = pages[j]
                other = grams(texts[p])
                shared = len(gold_grams & other) / max(1, len(gold_grams))
                kind = "other_file" if p[0] != g[0] else ("adjacent" if abs(p[1] - g[1]) == 1 else "same_file_far")
                above.append({"page": list(p), "kind": kind, "gold_text_shared": round(shared, 3), "score": round(float(dense[j]), 4)})
            rows.append({"id": t["id"], "category": t["category"], "gold_rank": rank, "gold_score": round(float(dense[pidx[g]]), 4),
                         "gold_chars": len(texts[g]), "above": above})
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)

    fails = [r for r in rows if r["gold_rank"] > 5]
    kinds = {}
    for r in fails:
        top = r["above"][0] if r["above"] else None
        k = "none" if top is None else top["kind"]
        kinds[k] = kinds.get(k, 0) + 1
    near_dup = [r["id"] for r in fails if any(a["gold_text_shared"] >= 0.5 for a in r["above"])]
    out = {"n": len(rows), "L2@5": sum(r["gold_rank"] <= 5 for r in rows), "failures": len(fails), "top1_above_gold_kind_counts": kinds,
           "failures_with_a_page_above_sharing_half_of_gold_text": near_dup,
           "gold_rank_distribution_of_failures": sorted(r["gold_rank"] for r in fails), "per_task": rows}
    json.dump(out, open("C:/Users/roy/kb/docs/s3_diag_gap.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "per_task"}, ensure_ascii=False))


main()
