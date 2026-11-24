"""S-1: A0b = existing wiki search made resident (index loaded once, query embedding via resident llama-server with the
small config -np 1 -c 4096). Measures end-to-end single-query latency over the 111 baseline queries (dense page-level
top-5) and resident memory of both processes. Run when the GPU is otherwise idle.
Run: PYTHONUTF8=1 python a0b_latency.py"""
import json
import os
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
PORT = 8796
NL = chr(10)


def rss_mb(pid):
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True)
    cell = r.stdout.strip().split('","')[-1].strip('"').replace(" K", "").replace(",", "")
    return round(int(cell) / 1024) if cell.isdigit() else None


def queries():
    qs = [x["query"] for x in json.load(open("C:/Users/roy/roy_km/scripts/eval_queries_real.json", encoding="utf-8"))["queries"]]
    for f in ("eval_queries_blind_v1.json", "eval_queries_blind_v2.json"):
        qs += [x["query"] for x in json.load(open("C:/Users/roy/roy_km/scripts/" + f, encoding="utf-8"))]
    return qs


def main():
    t = time.perf_counter()
    idx = we.load_index()
    ids = [c["id"] for pd in idx["pages"].values() for c in pd["chunks"]]
    M = np.array([c["embedding"] for pd in idx["pages"].values() for c in pd["chunks"]], dtype=np.float32)
    M /= np.linalg.norm(M, axis=1, keepdims=True)
    pages = [i.split("#", 1)[0] for i in ids]
    load_s = time.perf_counter() - t
    proc = subprocess.Popen([SERVER, "-m", MODEL, "--embeddings", "--pooling", "last", "-ngl", "99", "-np", "1", "-c", "4096",
                             "-b", "4096", "-ub", "4096", "--host", "127.0.0.1", "--port", str(PORT)],
                            stdout=subprocess.DEVNULL, stderr=open(HERE + "server6.log", "wb"))
    res = {"index_load_and_matrix_s": round(load_s, 2), "n_chunks": len(ids)}
    try:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 120:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.3)
        res["server_ready_s"] = round(time.perf_counter() - t0, 2)

        def embed(q):
            body = {"input": [f"Instruct: {we.QUERY_INSTRUCTION}{NL}Query:{q}".replace(NL, " ")], "model": "x"}
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/embeddings", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            v = np.array(json.loads(urllib.request.urlopen(req, timeout=60).read())["data"][0]["embedding"], dtype=np.float32)
            return v / np.linalg.norm(v)

        embed("warmup")
        lat = []
        for q in queries():
            s = time.perf_counter()
            sims = M @ embed(q)
            order = np.argsort(-sims)
            seen, top = set(), []
            for j in order:
                if pages[j] not in seen:
                    seen.add(pages[j])
                    top.append(pages[j])
                if len(top) == 5:
                    break
            lat.append((time.perf_counter() - s) * 1000)
        res["end_to_end_ms"] = {"n": len(lat), "median": round(float(np.median(lat)), 1), "p95": round(float(np.percentile(lat, 95)), 1),
                                "max": round(max(lat), 1)}
        res["server_rss_mb"] = rss_mb(proc.pid)
        res["python_rss_mb"] = rss_mb(os.getpid())
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    json.dump(res, open(HERE + "results/a0b_latency.json", "w", encoding="utf-8"), indent=1)
    print(json.dumps(res))


main()
