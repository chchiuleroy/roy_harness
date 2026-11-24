"""Corpus statistics for roy_km/_raw PDFs: pages, text-bearing pages, characters, estimated chunk count (800-char
chunks like the wiki indexer). Run: PYTHONUTF8=1 python pdf_corpus_stats.py"""
import json
import os
import time

import fitz

RAW = "C:/Users/roy/roy_km/_raw"
t0 = time.perf_counter()
tot = {"pdfs": 0, "failed": 0, "pages": 0, "text_pages": 0, "chars": 0, "no_text_pdfs": 0}
per = []
for f in sorted(os.listdir(RAW)):
    if not f.lower().endswith(".pdf"):
        continue
    tot["pdfs"] += 1
    try:
        doc = fitz.open(os.path.join(RAW, f))
    except Exception:
        tot["failed"] += 1
        continue
    chars = pages_t = 0
    for pg in doc:
        n = len(pg.get_text().strip())
        chars += n
        pages_t += int(n >= 200)
    tot["pages"] += doc.page_count
    tot["text_pages"] += pages_t
    tot["chars"] += chars
    if pages_t == 0:
        tot["no_text_pdfs"] += 1
    per.append({"pdf": f, "pages": doc.page_count, "text_pages": pages_t, "chars": chars})
    doc.close()
tot["est_chunks_800"] = tot["chars"] // 800
tot["extract_s"] = round(time.perf_counter() - t0, 1)
os.makedirs("C:/Users/roy/kb/s1/results", exist_ok=True)
json.dump({"totals": tot, "per_pdf": per}, open("C:/Users/roy/kb/s1/results/pdf_corpus_stats.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps(tot))
