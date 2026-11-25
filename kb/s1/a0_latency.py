"""A0 latency breakdown for the current wiki search: index load vs query embedding (llama-embedding spawn).
Read-only. Run: PYTHONUTF8=1 python a0_latency.py"""
import json
import statistics
import sys
import time

sys.path.insert(0, "C:/Users/roy/roy_km/scripts")
import wiki_embed as we

QUERIES = ["Hawkes process 維修排程", "KV cache 壓縮", "Qwen3 嵌入模型 量化", "威力彩 預測", "stanford one person frontier lab"]

t = time.perf_counter()
idx = we.load_index()
load_s = time.perf_counter() - t

embed_s = []
for q in QUERIES:
    t = time.perf_counter()
    we.encode_texts([q])
    embed_s.append(time.perf_counter() - t)

out = {
    "index_load_s": round(load_s, 3),
    "embed_query_s": [round(x, 3) for x in embed_s],
    "embed_median_s": round(statistics.median(embed_s), 3),
}
print(json.dumps(out, ensure_ascii=False))
