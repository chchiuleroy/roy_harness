"""Sample one text-bearing page from each of N randomly chosen PDFs under roy_km/_raw (seeded).
Writes tasks/pdf_pages_sample.json (page text excerpts for the blind query author) and
tasks/pdf_pages_gold_private.json (gold ids, never shown to the author beyond the neutral id).
Run: PYTHONUTF8=1 python make_pdf_pages.py"""
import hashlib
import json
import os
import re
import sys

import fitz  # PyMuPDF

HERE = "C:/Users/roy/kb/s1/"
RAW = "C:/Users/roy/roy_km/_raw"
SEED = 20261116
N = 48
MIN_CHARS, MAX_EXCERPT = 900, 1300
os.makedirs(HERE + "tasks", exist_ok=True)

import numpy as np

pdfs = sorted(os.path.join(RAW, f).replace(chr(92), "/") for f in os.listdir(RAW) if f.lower().endswith(".pdf"))
rng = np.random.default_rng(SEED)
order = list(rng.permutation(len(pdfs)))
picked = []
for i in order:
    path = pdfs[i]
    try:
        doc = fitz.open(path)
    except Exception as e:
        continue
    n = doc.page_count
    if n < 3:
        continue
    cand = list(rng.permutation(n))[:12]
    for pg in cand:
        txt = re.sub(r"\s+", " ", doc[int(pg)].get_text()).strip()
        low = txt.lower()
        if len(txt) < MIN_CHARS or low.count("references") > 0 or low.count("arxiv:") > 2:
            continue
        picked.append({"path": path, "page": int(pg) + 1, "n_pages": n, "text": txt[:MAX_EXCERPT],
                       "text_sha256": hashlib.sha256(txt.encode("utf-8")).hexdigest()})
        break
    doc.close()
    if len(picked) == N:
        break

public = [{"id": f"P{i+1:02d}", "excerpt": p["text"]} for i, p in enumerate(picked)]
private = [{"id": f"P{i+1:02d}", "gold_pdf": os.path.basename(p["path"]), "gold_page": p["page"], "n_pages": p["n_pages"],
            "text_sha256": p["text_sha256"]} for i, p in enumerate(picked)]
json.dump(public, open(HERE + "tasks/pdf_pages_sample.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
json.dump(private, open(HERE + "tasks/pdf_pages_gold_private.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(len(picked), "pages from", len({p["path"] for p in picked}), "pdfs of", len(pdfs))
