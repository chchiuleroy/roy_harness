"""PDF gap group (development group only), counting a wiki page that NAMES the paper as an answer too (Roy, 2026-11-19: "a wiki page counts").

The frozen measure (L1: the right FILE in the top 5) is not changed and stays the one that rules; this adds a second, descriptive one:
  L1+wiki = the top 5 (distinct file and page, as in the midterm) holds the gold PDF OR a wiki page whose text names the paper (its file stem or its arXiv id).
A page "names the paper" by a plain text match; a page that mentions a paper in passing counts as much as a page about it: the number is an upper bound.
Only the gap group is read (docs/s3_gap_tasks.json); no baseline set, no acceptance task.
Run: PYTHONUTF8=1 python tools/s4_gap_wiki_answer.py <parent folder of mid_home>        Output: docs/s4_gap_wiki_answer.json"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).replace(chr(92), "/")
sys.path.insert(0, ROOT + "/src")
sys.path.insert(0, "C:/Users/roy/roy_km/scripts")

from kb import conv_pdf, conv_registry, db, embedder  # noqa: E402
from kb.dense import Dense  # noqa: E402
from kb.home import Home  # noqa: E402
from kb.search import Searcher  # noqa: E402


def keys_of(pdf_name):
    stem = os.path.splitext(pdf_name)[0]
    found = {stem}
    m = re.search(r"(\d{4}\.\d{4,5})", stem)
    if m:
        found.add(m.group(1))
    return found


def main():
    import wiki_embed as we
    conv_registry.register(conv_pdf.PdfConverter())
    pages = {}
    for path in we.discover_pages():
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                pages[os.path.basename(path)] = f.read()
        except OSError:
            pass
    h = Home(os.path.join(sys.argv[1], "mid_home"))
    conn = db.connect(h.db_path, readonly=True)
    searcher = Searcher(h, conn, dense=Dense(embedder.LlamaServerEmbedder(h)))
    with open(ROOT + "/docs/s3_gap_tasks.json", encoding="utf-8") as f:
        tasks = json.load(f)["tasks"]
    out = {"n": len(tasks), "note": "L1 = frozen measure (the gold file in the top 5); L1_or_wiki also counts a wiki page that names the paper; an upper bound"}
    for mode in ("lexical", "dense", "hybrid"):
        l1 = l1w = 0
        wiki_only = []
        for t in tasks:
            gold = t["gold"]["pdf"]
            keys = keys_of(gold)
            seen, top = set(), []
            for hit in searcher.chunks(t["query"], 40, mode):
                key = (hit.name, hit.locator.get("page"))
                if key not in seen:
                    seen.add(key)
                    top.append(hit.name)
                if len(top) == 5:
                    break
            file_hit = gold in top
            wiki_hit = any(n.lower().endswith(".md") and any(k in pages.get(n, "") for k in keys) for n in top)
            l1 += int(file_hit)
            l1w += int(file_hit or wiki_hit)
            if wiki_hit and not file_hit:
                wiki_only.append(t["id"])
        out[mode] = {"L1": l1, "L1_or_wiki": l1w, "answered_only_by_a_wiki_page": wiki_only}
    with open(ROOT + "/docs/s4_gap_wiki_answer.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({m: {k: v for k, v in d.items() if k != "answered_only_by_a_wiki_page"} for m, d in out.items() if isinstance(d, dict)}))


if __name__ == "__main__":
    main()
