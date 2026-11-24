"""S-1: embed the roy_km/_raw PDF corpus with the resident llama-server (Qwen3-Embedding-0.6B Q8_0, same flags as the
wiki index) in 800-char windows, resumable (shards of 1000 chunks). Local-only server, killed at the end.
Outputs results/pdf_chunks.json (chunk -> pdf/page) and results/pdf_vecs/shard_XXXX.npy (float16, L2-normalised).
Run (background ok): PYTHONUTF8=1 python embed_pdf_corpus.py"""
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
PORT = 8793
WIN, MINTAIL, SHARD, BATCH, CONC = 800, 150, 1000, 16, 2
OUT = HERE + "results/pdf_vecs/"
os.makedirs(OUT, exist_ok=True)

# ponytail: pickle is this project's own cache (pdf_bm25_baseline.py); never load one from elsewhere
pages = pickle.load(open(HERE + "results/pdf_pages.pkl", "rb"))
chunks = []
for pdf, pg, text in pages:
    if len(text) < 50:
        continue
    parts = [text[i:i + WIN] for i in range(0, len(text), WIN)]
    if len(parts) > 1 and len(parts[-1]) < MINTAIL:
        tail = parts.pop()
        parts[-1] += tail
    for p in parts:
        chunks.append({"pdf": pdf, "page": pg, "text": p})
json.dump([{"pdf": c["pdf"], "page": c["page"], "n": len(c["text"])} for c in chunks],
          open(HERE + "results/pdf_chunks.json", "w", encoding="utf-8"))
print("chunks", len(chunks), flush=True)


def post(texts):
    texts = [t.replace(chr(10), " ") for t in texts]
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/embeddings", data=json.dumps({"input": texts, "model": "x"}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = sorted(json.loads(r.read())["data"], key=lambda d: d["index"])
    v = np.array([x["embedding"] for x in d], dtype=np.float32)
    if len(v) != len(texts):
        raise RuntimeError("embedding count mismatch")
    return v / np.linalg.norm(v, axis=1, keepdims=True)


proc = subprocess.Popen([SERVER, "-m", MODEL, "--embeddings", "--pooling", "last", "-ngl", "99", "-c", "8192", "-b", "8192",
                         "-ub", "8192", "--host", "127.0.0.1", "--port", str(PORT)], stdout=subprocess.DEVNULL,
                        stderr=open(HERE + "server3.log", "wb"))
try:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 120:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.3)
    start = time.perf_counter()
    done_chunks = 0
    for s in range(0, len(chunks), SHARD):
        path = OUT + f"shard_{s // SHARD:04d}.npy"
        if os.path.exists(path):
            continue
        part = [c["text"] for c in chunks[s:s + SHARD]]
        groups = [part[i:i + BATCH] for i in range(0, len(part), BATCH)]
        with cf.ThreadPoolExecutor(CONC) as ex:
            vecs = np.concatenate(list(ex.map(post, groups)))
        np.save(path + ".tmp.npy", vecs.astype(np.float16))
        os.replace(path + ".tmp.npy", path)
        done_chunks += len(part)
        el = time.perf_counter() - start
        print(f"shard {s // SHARD} done; {done_chunks} chunks this run, {el:.0f}s, {done_chunks / el:.1f}/s", flush=True)
finally:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
print("ALL DONE" if len([f for f in os.listdir(OUT) if f.endswith(".npy") and "tmp" not in f]) == -(-len(chunks) // SHARD) else "INCOMPLETE", flush=True)
