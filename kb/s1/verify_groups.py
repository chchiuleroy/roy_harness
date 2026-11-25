"""Recompute every task's group from the documented rule and compare with the task files.
PDF tasks use sha256("kb-s1-v1|" + P-id) and image tasks sha256("kb-s1-v1|IMG-" + I-id) (parity: 0 gap, 1 acceptance).
Exit 1 on any mismatch. Run: python verify_groups.py"""
import hashlib
import json
import sys

HERE = "C:/Users/roy/kb/s1/tasks/"
bad = []
for f, prefix, strip in (("pdf_tasks.json", "kb-s1-v1|", "PDF-"), ("image_tasks.json", "kb-s1-v1|IMG-", "IMG-")):
    for t in json.load(open(HERE + f, encoding="utf-8")):
        key = prefix + t["id"][len(strip):]
        want = "gap" if int(hashlib.sha256(key.encode()).hexdigest(), 16) % 2 == 0 else "acceptance"
        if t["group"] != want:
            bad.append((t["id"], t["group"], want))
print("MISMATCH" if bad else "OK: all groups reproduce from the hash rule", bad)
sys.exit(1 if bad else 0)
