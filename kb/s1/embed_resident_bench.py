"""S-1 embedding backend bench: resident llama-server (Vulkan, Qwen3-Embedding-0.6B Q8_0 GGUF) vs the current
spawn-per-call llama-embedding path, and agreement of vectors with the existing wiki index.
Queries use the same instruction prefix and newline flattening as wiki_embed.query/encode_texts.
Stored index vectors were built from FULL chunk text (the index only stores the first 400 chars), so the
agreement check regenerates full chunk text with wiki_embed.chunk_page.
Local-only server (127.0.0.1), killed by process tree at the end. Writes results/embed_resident.json.
Run: PYTHONUTF8=1 python embed_resident_bench.py"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import numpy as np

sys.path.insert(0, "C:/Users/roy/roy_km/scripts")
import wiki_embed as we

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = "C:/wcpp/llama.cpp/build/bin/Release/llama-server.exe"
MODEL = "C:/wcpp/models/Qwen3-Embedding-0.6B-Q8_0.gguf"
PORT = 8791
NL = chr(10)


def load_queries():
    qs = []
    d = json.load(open("C:/Users/roy/roy_km/scripts/eval_queries_real.json", encoding="utf-8"))
    qs += [x["query"] for x in d["queries"]]
    for f in ("eval_queries_blind_v1.json", "eval_queries_blind_v2.json"):
        qs += [x["query"] for x in json.load(open("C:/Users/roy/roy_km/scripts/" + f, encoding="utf-8"))]
    return qs


def instructed(q):
    return f"Instruct: {we.QUERY_INSTRUCTION}{NL}Query:{q}"


def post(path, payload, timeout=120):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def embed(texts):
    texts = [t.replace(NL, " ") for t in texts]          # same flattening as wiki_embed.encode_texts
    r = post("/v1/embeddings", {"input": texts, "model": "x"})
    v = np.array([d["embedding"] for d in sorted(r["data"], key=lambda d: d["index"])], dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def pct(a, q):
    return float(np.percentile(a, q))


def full_chunk_texts(ids):
    """Regenerate full chunk text for stored chunk ids via the indexer's own chunker."""
    out = {}
    wanted_pages = {}
    for cid in ids:
        wanted_pages.setdefault(cid.split("#", 1)[0], set()).add(cid)
    for page, want in wanted_pages.items():
        raw = open(os.path.join(we.WIKI, page + ".md"), encoding="utf-8").read()
        for cid, ctext in we.chunk_page(page, raw):
            if cid in want:
                out[cid] = ctext
    return out


def main():
    qs = load_queries()
    res = {"n_queries": len(qs)}
    t0 = time.perf_counter()
    proc = subprocess.Popen([SERVER, "-m", MODEL, "--embeddings", "--pooling", "last", "-ngl", "99",
                             "-c", "8192", "-b", "8192", "-ub", "8192", "--host", "127.0.0.1", "--port", str(PORT)],
                            stdout=subprocess.DEVNULL, stderr=open(os.path.join(HERE, "server.log"), "wb"))
    try:
        ready = None
        while time.perf_counter() - t0 < 120:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
                ready = time.perf_counter() - t0
                break
            except Exception:
                time.sleep(0.3)
        res["startup_to_ready_s"] = None if ready is None else round(ready, 2)
        if ready is None:
            raise RuntimeError("server not ready in 120s")
        embed(["warmup"])
        lat, vecs = [], []
        for q in qs:
            t = time.perf_counter()
            vecs.append(embed([instructed(q)])[0])
            lat.append((time.perf_counter() - t) * 1000)
        res["single_query_ms"] = {"median": round(pct(lat, 50), 1), "p95": round(pct(lat, 95), 1), "max": round(max(lat), 1)}
        V = np.stack(vecs)
        spawn = we.encode_texts([instructed(q) for q in qs[:12]])
        cos = (spawn * V[:12]).sum(axis=1)
        res["cos_query_server_vs_spawn"] = {"n": 12, "min": round(float(cos.min()), 6), "mean": round(float(cos.mean()), 6)}

        idx = we.load_index()
        stored_by_id = {c["id"]: c["embedding"] for pd in idx["pages"].values() for c in pd["chunks"]}
        rng = np.random.default_rng(20261116)
        ids = sorted(stored_by_id)
        pick = [ids[i] for i in rng.choice(len(ids), 40, replace=False)]
        ft = full_chunk_texts(pick)
        pick = [i for i in pick if i in ft]
        texts = [ft[i] for i in pick]
        stored = np.array([stored_by_id[i] for i in pick], dtype=np.float32)
        stored /= np.linalg.norm(stored, axis=1, keepdims=True)
        rv = embed(texts)
        c2 = (stored * rv).sum(axis=1)
        sp = we.encode_texts(texts)
        c3 = (stored * sp).sum(axis=1)
        res["cos_chunk_vs_stored_index"] = {"n": len(pick), "server_min": round(float(c2.min()), 6),
                                            "server_mean": round(float(c2.mean()), 6),
                                            "spawn_min": round(float(c3.min()), 6), "spawn_mean": round(float(c3.mean()), 6)}
        t = time.perf_counter()
        embed(texts * 2)
        res["batch_chunks_s"] = {"n": len(texts) * 2, "s": round(time.perf_counter() - t, 2)}
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    json.dump(res, open(os.path.join(HERE, "results", "embed_resident.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False))


main()
