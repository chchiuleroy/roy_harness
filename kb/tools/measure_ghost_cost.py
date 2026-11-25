"""S0 measurement: what does content-level ghost checking cost? (Gate 2 section 4.10: the manifest commits to a text hash and a
vector hash per chunk; a commit re-hashes them to prove the index content matches. If that is cheap, verify ALL chunks at
every commit instead of sampling.) Uses the real S-1 data: 31,980 PDF chunks and their float16 vectors.
Run: python tools/measure_ghost_cost.py   -> writes docs/s0_ghost_cost.json"""
import hashlib
import json
import os
import pickle
import statistics
import time

import numpy as np

S1 = "C:/Users/roy/kb/s1/results/"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
os.makedirs(OUT, exist_ok=True)

meta = json.load(open(S1 + "pdf_chunks.json", encoding="utf-8"))
shards = sorted(f for f in os.listdir(S1 + "pdf_vecs") if f.endswith(".npy") and "tmp" not in f)
vecs = np.concatenate([np.load(S1 + "pdf_vecs/" + f) for f in shards])
# ponytail: this pickle is the S-1 project's own cache (pdf_bm25_baseline.py); never load one from elsewhere
pages = pickle.load(open(S1 + "pdf_pages.pkl", "rb"))
texts = []
for _, _, t in pages:
    if len(t) < 50:
        continue
    parts = [t[i:i + 800] for i in range(0, len(t), 800)]
    if len(parts) > 1 and len(parts[-1]) < 150:
        tail = parts.pop()
        parts[-1] += tail
    texts += parts
assert len(texts) == len(vecs) == len(meta), (len(texts), len(vecs), len(meta))
n = len(texts)

runs = []
for _ in range(5):
    t0 = time.perf_counter()
    th = [hashlib.sha256(t.encode("utf-8")).digest() for t in texts]
    t1 = time.perf_counter()
    vh = [hashlib.sha256(vecs[i].tobytes()).digest() for i in range(n)]
    t2 = time.perf_counter()
    runs.append((t1 - t0, t2 - t1))
text_s = statistics.median(r[0] for r in runs)
vec_s = statistics.median(r[1] for r in runs)
res = {"n_chunks": n, "text_hash_s_median": round(text_s, 3), "vector_hash_s_median": round(vec_s, 3),
       "total_full_verify_s": round(text_s + vec_s, 3), "per_chunk_us": round((text_s + vec_s) / n * 1e6, 1),
       "extrapolated_1M_chunks_s": round((text_s + vec_s) / n * 1e6, 1), "runs": 5,
       "decision": "verify ALL chunks at every commit (content layer); sampling only if cost per 1M chunks exceeds 30 s",
       "note": "hash cost only; reading vectors from disk is not included (they were in memory). Single machine, 5 repeats."}
json.dump(res, open(os.path.join(OUT, "s0_ghost_cost.json"), "w"), indent=1)
print(json.dumps(res))
