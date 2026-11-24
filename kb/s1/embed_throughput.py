"""S-1: embedding throughput of the resident llama-server on 800-char PDF chunks, for batch sizes and client
concurrency. Bounds the cost of embedding the PDF corpus (est. ~29.5K chunks). Local server, killed at the end.
Run: PYTHONUTF8=1 python embed_throughput.py"""
import concurrent.futures as cf
import json
import os
import pickle
import subprocess
import time
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__)) + "/"
SERVER = "C:/wcpp/llama.cpp/build/bin/Release/llama-server.exe"
MODEL = "C:/wcpp/models/Qwen3-Embedding-0.6B-Q8_0.gguf"
PORT = 8792
# ponytail: the pickle is this project's own cache written by pdf_bm25_baseline.py; never load one from elsewhere
pages = pickle.load(open(HERE + "results/pdf_pages.pkl", "rb"))
rng = np.random.default_rng(7)
chunks = []
for i in rng.permutation(len(pages))[:400]:
    t = pages[int(i)][2]
    if len(t) >= 800:
        chunks.append(t[:800])
    if len(chunks) == 192:
        break


def post(texts):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/embeddings", data=json.dumps({"input": texts, "model": "x"}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return len(json.loads(r.read())["data"])


def run(batch, conc, total=96):
    parts = [chunks[i:i + batch] for i in range(0, total, batch)]
    t = time.perf_counter()
    with cf.ThreadPoolExecutor(conc) as ex:
        n = sum(ex.map(post, parts))
    s = time.perf_counter() - t
    return {"batch": batch, "concurrency": conc, "chunks": n, "s": round(s, 2), "chunks_per_s": round(n / s, 1)}


proc = subprocess.Popen([SERVER, "-m", MODEL, "--embeddings", "--pooling", "last", "-ngl", "99", "-c", "8192", "-b", "8192",
                         "-ub", "8192", "--host", "127.0.0.1", "--port", str(PORT)], stdout=subprocess.DEVNULL,
                        stderr=open(HERE + "server2.log", "wb"))
res = []
try:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 120:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.3)
    post(chunks[:4])
    for b, c in [(1, 1), (8, 1), (16, 1), (32, 1), (8, 2), (16, 2)]:
        res.append(run(b, c))
        print(res[-1], flush=True)
finally:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
json.dump({"chunk_chars": 800, "runs": res}, open(HERE + "results/embed_throughput.json", "w", encoding="utf-8"), indent=1)
