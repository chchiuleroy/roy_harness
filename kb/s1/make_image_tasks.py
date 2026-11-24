"""S-1: candidate image-page tasks from the scanned book (vertical classical Chinese). Maps silver-reference markdown
paragraphs to page images by Windows-OCR character-bigram overlap (OCR bigram recall on this material is only ~0.52,
so the mapping is NOT reliable enough for acceptance: the predicate below is overlap >= 0.5 and margin over the
second page >= 0.15, tasks are drawn at random from the candidates, and only 1 of the 18 drawn pages was checked
by eye; image tasks are therefore marked exploratory_silver). Writes tasks/image_candidates.json
(excerpts for the blind query author) and tasks/image_candidates_gold_private.json.
Run: PYTHONUTF8=1 python make_image_tasks.py"""
import collections
import glob
import json
import os
import re

import numpy as np

HERE = "C:/Users/roy/kb/s1/"
CJK = re.compile(r"[\u3400-\u9fff]")
ocr = json.load(open(HERE + "results/ocr_win_book_all.json", encoding="utf-8"))
pages = []
for pg in ocr["pages"]:
    img = pg["image"].replace(chr(92), "/")
    vol = "上" if "炁體源流_pages" in img else "下"
    pages.append({"vol": vol, "file": os.path.basename(img), "img": img,
                  "bi": set(zip(*(lambda cs: (cs, cs[1:]))([c for c in pg["text"] if CJK.match(c)])))})
print("ocr pages", len(pages))

paras = []
for f in sorted(glob.glob("C:/Users/roy/炁體源流_markdown/*/*.md")):
    for blk in re.split(r"\n\s*\n", open(f, encoding="utf-8").read()):
        blk = blk.strip()
        if blk.startswith(("#", ">", "---")):
            continue
        cs = [c for c in blk if CJK.match(c)]
        if len(cs) >= 70:
            paras.append({"md": os.path.basename(f), "text": blk[:400], "bi": set(zip(cs, cs[1:]))})
print("paragraphs", len(paras))

cands = []
for p in paras:
    sc = np.array([len(p["bi"] & q["bi"]) / max(1, len(p["bi"])) for q in pages])
    o = np.argsort(-sc)
    if sc[o[0]] >= 0.5 and sc[o[0]] - sc[o[1]] >= 0.15:
        cands.append({"para": p, "page": pages[o[0]], "score": float(sc[o[0]]), "margin": float(sc[o[0]] - sc[o[1]])})
print("confident", len(cands))
rng = np.random.default_rng(20261116)
pick = [cands[i] for i in rng.permutation(len(cands))[:18]]
pub = [{"id": f"I{i+1:02d}", "excerpt": c["para"]["text"]} for i, c in enumerate(pick)]
priv = [{"id": f"I{i+1:02d}", "gold_vol": c["page"]["vol"], "gold_page_file": c["page"]["file"], "image": c["page"]["img"],
         "md": c["para"]["md"], "ocr_overlap": round(c["score"], 3), "margin": round(c["margin"], 3)} for i, c in enumerate(pick)]
json.dump(pub, open(HERE + "tasks/image_candidates.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
json.dump(priv, open(HERE + "tasks/image_candidates_gold_private.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
for r in priv:
    print(r["id"], r["gold_vol"], r["gold_page_file"], r["ocr_overlap"], r["margin"])
