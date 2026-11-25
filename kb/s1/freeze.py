"""S-1 freeze: record sha256 of every file whose content must not change after S-1 (task sets, judging rules,
thresholds, A0 definition and per-query A0 outputs, write-side cases, baseline query sets, and the baseline code), plus
the rule that acceptance-group results may not be produced before S9.
  python freeze.py            writes freeze.json (refuses to overwrite an existing one)
  python freeze.py --verify   exit 1 if any hash differs, a frozen file is missing, or an unlisted file appears
                              in tasks/ or a0/ (an extra file could silently replace a frozen definition)"""
import glob
import hashlib
import json
import os
import sys
import time

HERE = "C:/Users/roy/kb/s1/"
EXTERNAL = [
    "C:/Users/roy/roy_km/scripts/eval_queries_real.json",
    "C:/Users/roy/roy_km/scripts/eval_queries_blind_v1.json",
    "C:/Users/roy/roy_km/scripts/eval_queries_blind_v2.json",
]
LOCAL_FIXED = ["judging_rules.md", "thresholds.json", "a0_definition.md", "pdf_dense_eval.py", "pdf_bm25_baseline.py",
               "image_eval.py", "make_pdf_pages.py", "make_image_tasks.py", "verify_groups.py", "a0_summary.py"]
GLOBS = ["tasks/*.json", "a0/*.json"]


def listed():
    files = list(LOCAL_FIXED)
    for g in GLOBS:
        files += [os.path.relpath(p, HERE).replace(os.sep, "/") for p in sorted(glob.glob(HERE + g))]
    # the author prompts and private gold files are inputs/outputs of task building, not part of the frozen definition
    files = [f for f in files if not f.endswith(("_private.json", "image_candidates.json", "pdf_pages_sample.json", "author_out.json",
                                                  "image_author_out.json"))]
    return files + EXTERNAL


def path_of(p):
    return p if p.startswith("C:") else HERE + p


def sha(p):
    return hashlib.sha256(open(path_of(p), "rb").read()).hexdigest()


def main():
    out = HERE + "freeze.json"
    if "--verify" in sys.argv:
        frozen = json.load(open(out, encoding="utf-8"))["sha256"]
        problems = []
        for p, h in frozen.items():
            if not os.path.exists(path_of(p)):
                problems.append(("missing", p))
            elif sha(p) != h:
                problems.append(("changed", p))
        for p in listed():
            if p not in frozen:
                problems.append(("unlisted", p))
        print("PROBLEMS:" if problems else "OK: all frozen files unchanged", problems)
        sys.exit(1 if problems else 0)
    if os.path.exists(out):
        sys.exit("freeze.json exists; refusing to overwrite (a freeze is one-time)")
    files = listed()
    rec = {"frozen_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "version": 2,
           "sha256": {p: sha(p) for p in files},
           "rule": "No kb result on group=acceptance tasks may be produced before S9. Any change to a listed file after this point must be announced to Roy as a change to the frozen definition, with the reason."}
    json.dump(rec, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("frozen", len(files), "files")


main()
