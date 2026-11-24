"""Cut the PDF GAP group out of the frozen PDF task file into its own file, so that the midterm evaluation never opens a file that holds the
acceptance group (a filter inside the evaluator still reads those records; this tool is the only place that does, and it prints counts only).

Run once: python tools/s3_make_gap_tasks.py        Output: docs/s3_gap_tasks.json (refuses to overwrite)."""
import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).replace(chr(92), "/")
SRC = ROOT + "/s1/tasks/pdf_tasks.json"
OUT = ROOT + "/docs/s3_gap_tasks.json"


def main():
    if os.path.exists(OUT):
        raise SystemExit(f"refused: {OUT} exists")
    raw = open(SRC, "rb").read()
    tasks = json.loads(raw.decode("utf-8"))
    gap = [t for t in tasks if t["group"] == "gap"]
    digest = hashlib.sha256(json.dumps(gap, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()   # same form as the evaluator's
    out = {"derived_from": "s1/tasks/pdf_tasks.json", "derived_from_sha256": hashlib.sha256(raw).hexdigest(), "group": "gap", "tasks": gap,
           "tasks_sha256": digest}
    with open(OUT, "x", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"wrote {len(gap)} gap tasks of {len(tasks)}; groups seen: {sorted({t['group'] for t in tasks})}")


main()
