"""Diagnosis only: (1) does my re-implementation of A0's BM25 agree with A0's saved per-query result? (2) how much does corpus drift alone move the
same system's Success@5? A0's results were saved at 08:41 on 2026-11-16 over 871 pages / 7,652 chunks; kb was evaluated later over 878 pages /
7,706 A0-style chunks, because the wiki was edited in between. Reuses the functions of s1_diag_decompose.py. Nothing is tuned.
Run: PYTHONUTF8=1 python tools/s1_diag_drift.py <parent that holds wiki_home>"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s1_diag_decompose as dd  # noqa: E402

we, tk, ROOT = dd.we, dd.tk, dd.ROOT
A0F = dd.FORMULAS["A0 (k1=1.5, idf=log(1+x))"]
SAVED_AT = os.path.getmtime(os.path.join(ROOT, "s1", "a0", "blind2.json"))


def a0_chunks(skip_modified):
    out = []
    for f in sorted(we.discover_pages()):
        if skip_modified and os.path.getmtime(f) > SAVED_AT:
            continue
        name = os.path.splitext(os.path.basename(f))[0]
        for cname, text in we.chunk_page(name, open(f, encoding="utf-8").read()):
            out.append((cname.split("#")[0], we.bm25_tokens(text)))
    return out


def rows(name):
    return [r for r in json.load(open(os.path.join(ROOT, "s1", "a0", name + ".json"), encoding="utf-8"))["results"]
            if r["gold"] and not r["gold_missing_from_index"]]


full, trimmed = dd.build_index(a0_chunks(False)), dd.build_index(a0_chunks(True))
print("pages/chunks full:", len({p for p, _ in full[0]}), len(full[0]), " without files edited after the A0 run:", len({p for p, _ in trimmed[0]}), len(trimmed[0]))
res = {}
for name in ("real", "blind1", "blind2"):
    same5 = same1 = n = moved5 = 0
    hits_full = hits_trim = hits_saved = used = 0
    for r in rows(name):
        q = tk.tokens(r["query"])
        a = dd.page_ranks(full, q, A0F)
        b = dd.page_ranks(trimmed, q, A0F)
        saved = r["bm25"]
        n += 1
        same5 += set(a[:5]) == set(saved[:5])
        same1 += a[:1] == saved[:1]
        moved5 += set(a[:5]) != set(b[:5])
        g = set(r["gold"])
        if g & {p for p, _ in trimmed[0]}:                     # only queries whose gold page was not edited afterwards
            used += 1
            hits_full += bool(g & set(a[:5]))
            hits_trim += bool(g & set(b[:5]))
            hits_saved += bool(g & set(saved[:5]))
    res[name] = dict(n=n, top5_same_as_saved=same5, top1_same_as_saved=same1, top5_changed_by_dropping_edited_files=moved5, gold_unedited=used,
                     hits_full=hits_full, hits_trimmed=hits_trim, hits_saved=hits_saved)
    print(name, res[name])
json.dump(res, open(os.path.join(ROOT, "docs", "s1_diag_drift.json"), "w", encoding="utf-8"), indent=1)
