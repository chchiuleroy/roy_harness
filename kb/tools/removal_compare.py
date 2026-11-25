"""Behavioural evidence for OPT1: run the harness's own full test suite twice, once with kb importable (PYTHONPATH includes kb/src)
and once without, and require the per-test outcomes to be identical. Slow (the harness suite takes about 11 minutes each run).
Uses the harness's own runner (tests/superagent_removal_runner.py), which prints {test id: outcome}. Nested self-checks of the
harness (SUPERAGENT_REMOVAL) and live-model tests are switched off, as that repo's own notes require.
Run: python tools/removal_compare.py [harness checkout root]  -> docs/s0_removal_compare.json"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


def run(harness_root, with_kb):
    env = {k: v for k, v in os.environ.items() if k not in ("SUPERAGENT_REMOVAL", "HARNESS_LIVE", "SUPERAGENT_LIVE", "KB_FAULT")}
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = SRC if with_kb else ""
    t = time.time()
    r = subprocess.run([sys.executable, os.path.join(harness_root, "tests", "superagent_removal_runner.py"), harness_root],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=3000)
    line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    return json.loads(line), round(time.time() - t)


def main():
    harness_root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "C:/Users/roy/roy_harness")
    without, t0 = run(harness_root, False)
    with_kb, t1 = run(harness_root, True)
    diff = sorted(k for k in set(without) | set(with_kb) if without.get(k) != with_kb.get(k))
    counts = {}
    for v in without.values():
        counts[v] = counts.get(v, 0) + 1
    res = {"tests": len(without), "outcomes_without_kb": counts, "seconds": [t0, t1], "identical": not diff and bool(without), "differences": diff[:20]}
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    json.dump(res, open(os.path.join(ROOT, "docs", "s0_removal_compare.json"), "w"), indent=1)
    print(json.dumps(res))


main()
