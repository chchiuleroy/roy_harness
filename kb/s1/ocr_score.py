"""Score Windows OCR output against the silver reference markdown (provenance of the markdown is unknown, so this is
NOT gold). Order-free, search-oriented measures on character bigrams (what a CJK bigram BM25 index needs).
Run after: powershell -File ocr_winrt.ps1 -Out results/ocr_win_p024_p040.json -Lang auto <images...>
Run: PYTHONUTF8=1 python ocr_score.py"""
import collections
import json
import re
import sys

HERE = "C:/Users/roy/kb/s1/"
ref = open("C:/Users/roy/炁體源流_markdown/上冊/01_太清元道真經註解.md", encoding="utf-8").read()
ocr = json.load(open(HERE + "results/ocr_win_p024_p040.json", encoding="utf-8"))
CJK = re.compile(r"[\u3400-\u9fff]")


def chars(s):
    return [c for c in s if CJK.match(c)]


def bigrams(cs):
    return collections.Counter(zip(cs, cs[1:]))


# body of the reference only (drop the markdown header block that is not on the page)
body = ref.split("---", 2)[-1]
rc = chars(body)
rb = bigrams(rc)
rset = set(rb)
rchars = collections.Counter(rc)

tot_chars = tot_hit_chars = tot_bi = tot_hit_bi = 0
all_oc = []
per = []
for pg in ocr["pages"]:
    oc = chars(pg["text"])
    all_oc += oc
    ob = bigrams(oc)
    hit_b = sum(n for b, n in ob.items() if b in rset)
    n_b = sum(ob.values())
    hit_c = sum(min(n, rchars[c]) for c, n in collections.Counter(oc).items())
    per.append({"image": pg["image"].split("\\")[-1], "ms": pg["ms"], "cjk_chars": len(oc),
                "char_precision": round(hit_c / max(1, len(oc)), 3),
                "bigram_precision": round(hit_b / max(1, n_b), 3)})
    tot_chars += len(oc)
    tot_hit_chars += hit_c
    tot_bi += n_b
    tot_hit_bi += hit_b

ob_all = bigrams(all_oc)
bi_recall = sum(min(n, ob_all[b]) for b, n in rb.items()) / max(1, sum(rb.values()))
ch_recall = sum(min(n, collections.Counter(all_oc)[c]) for c, n in rchars.items()) / max(1, len(rc))
res = {"ref_cjk_chars": len(rc), "ocr_cjk_chars": len(all_oc),
       "char_recall_doc": round(ch_recall, 3), "bigram_recall_doc": round(bi_recall, 3),
       "char_precision": round(tot_hit_chars / max(1, tot_chars), 3),
       "bigram_precision": round(tot_hit_bi / max(1, tot_bi), 3),
       "ms_per_page_median": sorted(p["ms"] for p in per)[len(per) // 2], "pages": per,
       "reference_note": "silver, provenance unknown; page range 24-40 maps to this markdown file per its header"}
json.dump(res, open(HERE + "results/ocr_win_score.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps({k: v for k, v in res.items() if k != "pages"}, ensure_ascii=False))
