"""E1 latency after the cached original check: 100 neutral made-up queries on the midterm home (no task, no baseline query is read),
hybrid mode, the source view and the 40-hit chunk view, each after a warm-up. Run on a quiet machine (nothing else heavy running).
Usage: python tools/s4_latency.py <home> <output.json>   (the home must already be at the current schema: run `kb init` on it first)"""
import json, os, statistics, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from kb import conv_pdf, conv_registry, db, embedder
from kb.dense import Dense
from kb.home import Home
from kb.search import Searcher

TOPICS = ["gardens", "tides", "lanterns", "weather machines", "river barges", "clockwork birds", "salt markets", "winter orchards", "paper boats",
          "lighthouses", "copper bells", "mountain trains", "glass bridges", "night ferries", "moss libraries", "kite makers", "harbour cranes",
          "tea merchants", "stone gardens", "bell towers"]


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1)


def main(home_path, out_path):
    conv_registry.register(conv_pdf.PdfConverter())
    h = Home(home_path)
    conn = db.connect(h.db_path, readonly=True)
    emb = embedder.LlamaServerEmbedder(h)
    s = Searcher(h, conn, dense=Dense(emb))
    queries = [f"neutral timing probe number {i} about {TOPICS[i % len(TOPICS)]} and {TOPICS[(i * 7 + 3) % len(TOPICS)]}" for i in range(100)]
    for q in queries[:3]:                      # warm-up: the server, the page cache, the vector file
        s.sources(q + " warm", 5, "hybrid")
        s.chunks(q + " warm", 40, "hybrid")
    lat_source, lat_chunk, lat_chunk6, lat_chunk10, lat_embed = [], [], [], [], []
    for q in queries:
        t = time.perf_counter(); s.sources(q, 5, "hybrid"); lat_source.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter(); s.chunks(q, 40, "hybrid"); lat_chunk.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter(); s.chunks(q, 6, "hybrid"); lat_chunk6.append((time.perf_counter() - t) * 1000)      # the size `kb ask` gives the model
        t = time.perf_counter(); s.chunks(q, 10, "hybrid"); lat_chunk10.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter(); emb.embed_query(q + " alone"); lat_embed.append((time.perf_counter() - t) * 1000)
    res = {"home": "midterm home (882 wiki pages + 222 PDFs)", "n_queries": len(queries), "mode": "hybrid", "cached_original_check": True,
           "source_view": {"median": pct(lat_source, 0.5), "p95": pct(lat_source, 0.95), "max": round(max(lat_source), 1)},
           "chunk_view_40": {"median": pct(lat_chunk, 0.5), "p95": pct(lat_chunk, 0.95), "max": round(max(lat_chunk), 1)},
           "chunk_view_6": {"median": pct(lat_chunk6, 0.5), "p95": pct(lat_chunk6, 0.95), "max": round(max(lat_chunk6), 1)},
           "chunk_view_10": {"median": pct(lat_chunk10, 0.5), "p95": pct(lat_chunk10, 0.95), "max": round(max(lat_chunk10), 1)},
           "query_embedding_alone": {"median": pct(lat_embed, 0.5), "p95": pct(lat_embed, 0.95)},
           "mean_source_view": round(statistics.mean(lat_source), 1)}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res, indent=1))
    emb.stop()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
