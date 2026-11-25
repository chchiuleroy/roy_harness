"""S3 midterm, step 1: build the MIXED kb home (the roy_km wiki plus the PDFs of roy_km/_raw), embed every chunk, publish one commit with vectors.
Resumable: run it again after an interruption and it continues where the vector cache stopped. Read-only on Roy's data (wiki files are copied in,
PDFs are kept where they are). It touches no query set and no result: building is not evaluating.
Run: PYTHONUTF8=1 python tools/s3_midterm_build.py <parent folder for the home>        Output: docs/s3_midterm_build.json"""
import json
import os
import sys
import time

ROOT = "C:/Users/roy/kb"
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, "C:/Users/roy/roy_km/scripts")

from kb import cas, colls, commit, conv_pdf, conv_registry, db, embedder, inbox, pipeline, relations, vecindex  # noqa: E402
from kb.home import Home  # noqa: E402

RAW = "C:/Users/roy/roy_km/_raw"
SERVER = "C:/wcpp/llama.cpp/build/bin/Release/llama-server.exe"
MODEL = "C:/wcpp/models/Qwen3-Embedding-0.6B-Q8_0.gguf"


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def main():
    import wiki_embed as we
    conv_registry.register(conv_pdf.PdfConverter())
    h = Home(os.path.join(sys.argv[1], "mid_home"))
    h.init()
    report_path = os.path.join(ROOT, "docs", "s3_midterm_build.json")
    rep = json.load(open(report_path, encoding="utf-8")) if os.path.isfile(report_path) else {}
    conn = db.connect(h.db_path)
    store = cas.BlobStore(h.sub("blobs"))
    t0 = time.perf_counter()
    if not conn.execute("SELECT count(*) FROM collections").fetchone()[0]:      # intake is in the database: a rerun must not repeat it
        wiki_coll, pdf_coll = colls.create(conn, "wiki"), colls.create(conn, "pdf")
        files = sorted(we.discover_pages())
        outcomes = {}
        for r in [inbox.Intake(conn, store, wiki_coll, settle_s=0).add_file(p) for p in files]:
            outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
        pdfs = sorted(os.path.join(RAW, f).replace(chr(92), "/") for f in os.listdir(RAW) if f.lower().endswith(".pdf"))
        pdf_intake = inbox.Intake(conn, store, pdf_coll, settle_s=0)
        pdf_outcomes = {}
        for r in [pdf_intake.add_file(p, "ref") for p in pdfs]:
            pdf_outcomes[r.outcome] = pdf_outcomes.get(r.outcome, 0) + 1
        rep.update({"intake_done": True, "wiki_files": len(files), "wiki_outcomes": outcomes, "pdf_files": len(pdfs), "pdf_outcomes": pdf_outcomes,
                    "seconds_intake": round(time.perf_counter() - t0, 1)})
        say(f"intake: {len(files)} wiki pages, {len(pdfs)} pdfs")
    t1 = time.perf_counter()
    done = pipeline.process_pending(conn, store)
    if True:                                                      # statistics come from the database, so a rerun reports them too
        done = [type("D", (), {"status": r[0], "reason": r[1]})() for r in conn.execute("SELECT status, report_json FROM conversions")]
        failed = {}
        for d in done:
            if d.status == "failed":
                failed[d.reason] = failed.get(d.reason, 0) + 1
        rep["conversion"] = {k: sum(1 for d in done if d.status == k) for k in ("ok", "partial", "failed")}
        rep["failed_reasons"] = failed
        rep["pdf_conversions"] = {"status": [list(r) for r in conn.execute("SELECT status, count(*) FROM conversions WHERE converter='pdf' GROUP BY status")],
                                  "pages_without_text": conn.execute("SELECT COALESCE(SUM(json_extract(report_json,'$.pages_without_text')),0) FROM conversions WHERE converter='pdf'").fetchone()[0],
                                  "pages": conn.execute("SELECT COALESCE(SUM(json_extract(report_json,'$.pages')),0) FROM conversions WHERE converter='pdf'").fetchone()[0]}
        rep["seconds_convert"] = round(time.perf_counter() - t1, 1)
        say(f"converted {len(done)} revisions: {rep['conversion']}")
    relations.derive(conn)
    if not embedder.load_config(h):
        embedder.pin(h, SERVER, MODEL)
    emb = embedder.LlamaServerEmbedder(h)
    ident = emb.identity
    asm = commit.assemble(conn)
    cache = vecindex.VectorCache(vecindex.cache_path(h))
    todo = vecindex.missing(cache, ident, asm)
    say(f"{len(asm.chunks)} chunks, {len(vecindex.chunk_shas(asm))} distinct texts, {len(todo)} to embed")
    rep["chunks"], rep["distinct_texts"] = len(asm.chunks), len(vecindex.chunk_shas(asm))
    t2 = time.perf_counter()
    last = [time.perf_counter()]

    def progress(n, total):
        if time.perf_counter() - last[0] > 120 or n == total:
            last[0] = time.perf_counter()
            say(f"  embedded {n}/{total} ({n / (time.perf_counter() - t2):.1f}/s)")
    vecindex.embed_missing(cache, emb, ident, todo, progress, step=64)
    cache.close()
    rep["seconds_embed_this_run"] = round(time.perf_counter() - t2, 1)
    rep["embedded_this_run"] = len(todo)
    t3 = time.perf_counter()
    built = commit.build(h, conn, extensions=[vecindex.VectorExtension(h, emb, max_inline=0)])
    rep["commit"] = {"id": built.commit_id, "outcome": built.outcome, "seconds": round(time.perf_counter() - t3, 1)}
    seg = commit.segment_dir(h, built.commit_id)
    rep["sizes_bytes"] = {n: os.path.getsize(os.path.join(seg, n)) for n in os.listdir(seg)}
    rep["server"] = emb.status()
    json.dump(rep, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    say("done: " + json.dumps(rep["commit"]))
    conn.close()


main()
