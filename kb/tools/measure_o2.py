"""S0 measurement: timing noise of the harness import, to freeze the OPT2 tolerance (Gate 2: 'startup time difference no more than N
times the measurement noise; N is frozen after S0 measures it').

Method: alternate S_none (kb not importable) and S_installed (kb on the path) runs of tools/r2_probe.py, 40 each, take the import time.
Noise = median absolute difference from the median (MAD, scaled by 1.4826 to be comparable with a standard deviation) of the S_none runs.
N is the smallest integer for which random half-splits of the S_none runs themselves would exceed N x noise on the median difference in
at most 5 percent of 2000 splits (the false-alarm rate of the rule when nothing differs). The rule is then applied to S_installed vs S_none.
Run on a quiet machine: python tools/measure_o2.py -> docs/s0_o2_noise.json"""
import json
import os
import statistics
import subprocess
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
PROBE = os.path.join(ROOT, "tools", "r2_probe.py")
HARNESS = os.environ.get("KB_HARNESS_DIR", "C:/Users/roy/roy_harness/src/harness")
RUNS = 40


def one(installed, wd):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=SRC if installed else "")
    r = subprocess.run([sys.executable, PROBE, HARNESS, wd], capture_output=True, text=True, encoding="utf-8", env=env, check=True)
    return json.loads(r.stdout.strip().splitlines()[-1])["import_s"] * 1000


def main():
    wd = tempfile.mkdtemp(prefix="kb_o2_")
    none, inst = [], []
    one(False, wd), one(True, wd)                      # warm file caches
    for _ in range(RUNS):
        none.append(one(False, wd))
        inst.append(one(True, wd))
    med_n, med_i = statistics.median(none), statistics.median(inst)
    noise = 1.4826 * statistics.median(abs(x - med_n) for x in none)
    rng = np.random.default_rng(20261116)
    arr = np.array(none)
    chosen = None
    rates = {}
    for n in range(1, 11):
        bad = 0
        for _ in range(2000):
            p = rng.permutation(len(arr))
            a, b = arr[p[:len(arr) // 2]], arr[p[len(arr) // 2:]]
            bad += abs(np.median(a) - np.median(b)) > n * noise
        rates[n] = bad / 2000
        if chosen is None and rates[n] <= 0.05:
            chosen = n
    tol = (chosen or 10) * noise
    diff = med_i - med_n
    res = {"runs_each": RUNS, "median_none_ms": round(med_n, 1), "median_installed_ms": round(med_i, 1), "diff_ms": round(diff, 2),
           "noise_mad_scaled_ms": round(noise, 2), "false_alarm_rate_by_N": {str(k): v for k, v in rates.items()},
           "N_frozen": chosen, "tolerance_ms": round(tol, 2), "within_tolerance": abs(diff) <= tol,
           "note": "import time of the harness only (not a full session start); single machine; quiet-machine assumption"}
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    json.dump(res, open(os.path.join(ROOT, "docs", "s0_o2_noise.json"), "w"), indent=1)
    print(json.dumps(res))


main()
