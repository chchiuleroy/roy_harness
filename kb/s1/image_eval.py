"""S-1: image-page task baselines, GAP GROUP ONLY (set KB_S9=1 to include acceptance). Index = Windows built-in OCR text
of each page image (spaces between CJK characters removed). Systems: bm25 (CJK bigram), dense (Qwen3-Embedding resident),
hybrid (RRF k=60). Gold: (volume, image file), tolerance 0 pages. Run: PYTHONUTF8=1 python image_eval.py"""
import collections
import json
import math
import os
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
PORT = 8795
NL = chr(10)
LATIN, CJK = re.compile(r"[a-z0-9]+"), re.compile(r"[\u4e00-\u9fff]+")
GROUPS = {"gap", "acceptance"} if os.environ.get("KB_S9") == "1" else {"gap"}


def tok(s):
    out = [t for t in LATIN.findall(s.lower()) if len(t) >= 2 or t.isdigit()]
    for run in CJK.findall(s):
        out += [run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)]
    return out


def clean(t):
    return re.sub(r"(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff])", "", t)


def post(texts):
    body = {"input": [t.replace(NL, " ") for t in texts], "model": "x"}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/embeddings", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    d = sorted(json.loads(urllib.request.urlopen(req, timeout=600).read())["data"], key=lambda d: d["index"])
    v = np.array([x["embedding"] for x in d], dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def main():
    ocr = json.load(open(HERE + "results/ocr_win_book_all.json", encoding="utf-8"))["pages"]
    keys = [("上" if "炁體源流_pages" in p["image"] else "下", os.path.basename(p["image"].replace(chr(92), "/"))) for p in ocr]
    texts = [clean(p["text"]) for p in ocr]
    docs = [collections.Counter(tok(t)) for t in texts]
    lens = np.array([max(1, sum(c.values())) for c in docs], dtype=np.float64)
    N = len(docs)
    df = collections.Counter()
    for c in docs:
        df.update(c.keys())
    idf = {t: math.log(1 + (N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def bm25(q):
        sc = np.zeros(N)
        for t in set(tok(q)):
            for j, c in enumerate(docs):
                n = c.get(t, 0)
                if n:
                    sc[j] += idf[t] * n * 2.5 / (n + 1.5 * (0.25 + 0.75 * lens[j] / lens.mean()))
        return sc

    tasks = [t for t in json.load(open(HERE + "tasks/image_tasks.json", encoding="utf-8")) if t["group"] in GROUPS]
    proc = subprocess.Popen([SERVER, "-m", MODEL, "--embeddings", "--pooling", "last", "-ngl", "99", "-np", "1", "-c", "4096",
                             "-b", "4096", "-ub", "4096", "--host", "127.0.0.1", "--port", str(PORT)],
                            stdout=subprocess.DEVNULL, stderr=open(HERE + "server5.log", "wb"))
    rows = []
    try:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 120:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.3)
        D = np.concatenate([post([t[:1500] for t in texts[i:i + 16]]) for i in range(0, N, 16)])
        for t in tasks:
            qv = post([f"Instruct: {we.QUERY_INSTRUCTION}{NL}Query:{t['query']}"])[0]
            dense = D @ qv
            sparse = bm25(t["query"])
            lists = {"bm25": [keys[j] for j in np.argsort(-sparse)[:10] if sparse[j] > 0],
                     "dense": [keys[j] for j in np.argsort(-dense)[:10]]}
            rr = collections.defaultdict(float)
            for lst in lists.values():
                for r, p in enumerate(lst):
                    rr[p] += 1 / (61 + r)
            lists["hybrid"] = [p for p, _ in sorted(rr.items(), key=lambda x: -x[1])][:10]
            g = (t["gold"]["volume"], t["gold"]["image"])
            rec = {"id": t["id"], "group": t["group"], "category": t["category"]}
            for s, lst in lists.items():
                rec[s] = {"L2": g in lst[:5], "top1": lst[:1] == [g]}
            rows.append(rec)
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)

    def agg(rs, s):
        return {"n": len(rs), "L2@5": sum(r[s]["L2"] for r in rs), "L2@1": sum(r[s]["top1"] for r in rs)}

    out = {"groups_evaluated": sorted(GROUPS), "n_pages": N, "overall": {s: agg(rows, s) for s in ("bm25", "dense", "hybrid")},
           "by_category": {c: {s: agg([r for r in rows if r["category"] == c], s) for s in ("bm25", "dense", "hybrid")}
                           for c in sorted({r["category"] for r in rows})}, "per_task": rows}
    json.dump(out, open(HERE + "results/image_eval_gap.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "per_task"}, ensure_ascii=False))


main()
