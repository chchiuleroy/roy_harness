"""S-1: (1) Qwen3-Embedding-0.6B on CPU via sentence_transformers (HF cache, offline): resident query latency and
agreement with the GGUF index vectors; (2) all-MiniLM-L6-v2 (English-oriented) as an alternative embedding on the
same wiki chunks, scored with the same page-level Success@5 as the existing eval (gold from the query files).
Offline only. Writes results/embed_cpu_alt.json. Run: PYTHONUTF8=1 python embed_cpu_alt.py"""
import json
import os
import statistics
import sys
import time

os.environ["HF_HUB_OFFLINE"] = "1"
import numpy as np

sys.path.insert(0, "C:/Users/roy/roy_km/scripts")
import wiki_embed as we

HERE = os.path.dirname(os.path.abspath(__file__))
NL = chr(10)
SETS = {
    "real": ("eval_queries_real.json", "queries", "target_pages"),
    "blind1": ("eval_queries_blind_v1.json", None, "gold_pages"),
    "blind2": ("eval_queries_blind_v2.json", None, "gold_pages"),
}


def load_sets():
    out = {}
    for name, (f, key, gk) in SETS.items():
        d = json.load(open("C:/Users/roy/roy_km/scripts/" + f, encoding="utf-8"))
        rows = d[key] if key else d
        rows = [r for r in rows if r.get(gk)]
        out[name] = [(r["query"], set(r[gk])) for r in rows]
    return out


def instructed(q):
    return f"Instruct: {we.QUERY_INSTRUCTION}{NL}Query:{q}"


def all_chunks():
    idx = we.load_index()
    ids, texts = [], []
    for page in idx["pages"]:
        raw = open(os.path.join(we.WIKI, page + ".md"), encoding="utf-8").read()
        for cid, ctext in we.chunk_page(page, raw):
            ids.append(cid)
            texts.append(ctext)
    return ids, texts


def success5(qvecs, cvecs, ids, golds):
    pages = [i.split("#", 1)[0] for i in ids]
    hit = 0
    for qv, gold in zip(qvecs, golds):
        sims = cvecs @ qv
        order = np.argsort(-sims)
        seen, top = set(), []
        for j in order:
            p = pages[j]
            if p not in seen:
                seen.add(p)
                top.append(p)
            if len(top) == 5:
                break
        hit += int(any(g in top for g in gold))
    return hit


def main():
    from sentence_transformers import SentenceTransformer
    sets = load_sets()
    res = {}
    ids, texts = all_chunks()
    res["n_chunks"] = len(ids)

    # (1) Qwen3 on CPU, resident
    t = time.perf_counter()
    qm = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B", device="cpu")
    res["qwen_cpu_load_s"] = round(time.perf_counter() - t, 1)
    qs = [q for rows in sets.values() for q, _ in rows][:40]
    qm.encode([instructed(qs[0])])
    lat = []
    for q in qs:
        t = time.perf_counter()
        qm.encode([instructed(q)], normalize_embeddings=True)
        lat.append((time.perf_counter() - t) * 1000)
    res["qwen_cpu_single_query_ms"] = {"n": len(qs), "median": round(statistics.median(lat), 1),
                                       "p95": round(float(np.percentile(lat, 95)), 1)}
    idx = we.load_index()
    stored_by_id = {c["id"]: c["embedding"] for pd in idx["pages"].values() for c in pd["chunks"]}
    sample = [i for i in range(0, len(ids), 190)][:40]
    sv = np.array([stored_by_id[ids[i]] for i in sample], dtype=np.float32)
    sv /= np.linalg.norm(sv, axis=1, keepdims=True)
    cv = qm.encode([texts[i].replace(NL, " ") for i in sample], normalize_embeddings=True)
    res["qwen_cpu_cos_vs_gguf_index"] = {"n": len(sample), "min": round(float((sv * cv).sum(1).min()), 4),
                                         "mean": round(float((sv * cv).sum(1).mean()), 4)}
    del qm

    # (2) MiniLM on the same chunks
    t = time.perf_counter()
    mm = SentenceTransformer("C:/Users/roy/retrieval_pareto/data/models/all-MiniLM-L6-v2", device="cpu")
    C = mm.encode([x.replace(NL, " ") for x in texts], batch_size=64, normalize_embeddings=True)
    res["minilm_encode_corpus_s"] = round(time.perf_counter() - t, 1)
    res["minilm_max_seq_length"] = mm.max_seq_length
    lat = []
    for q in qs:
        t = time.perf_counter()
        mm.encode([q], normalize_embeddings=True)
        lat.append((time.perf_counter() - t) * 1000)
    res["minilm_single_query_ms"] = {"median": round(statistics.median(lat), 1), "p95": round(float(np.percentile(lat, 95)), 1)}
    res["success_at_5"] = {}
    for name, rows in sets.items():
        qv = mm.encode([q for q, _ in rows], normalize_embeddings=True)
        hit = success5(qv, C, ids, [g for _, g in rows])
        a0 = json.load(open(os.path.join(HERE, "a0", name + ".json"), encoding="utf-8"))["results"]
        gold_ok = [r for r in a0 if r["gold"] and not r["gold_missing_from_index"]]
        a0_hit = sum(int(any(g in r["vector"][:5] for g in r["gold"])) for r in gold_ok)
        res["success_at_5"][name] = {"n": len(rows), "minilm": hit, "qwen_gguf_A0_vector": a0_hit, "a0_n": len(gold_ok)}
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    json.dump(res, open(os.path.join(HERE, "results", "embed_cpu_alt.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False))


main()
