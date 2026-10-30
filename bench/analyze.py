"""Summarise one results folder, or compare two (paired by task).

    python bench/analyze.py bench_runs/base
    python bench/analyze.py bench_runs/c1 --vs bench_runs/base --splits val

Pairing unit is the task (family, seed); a task's score is its mean over repetitions. The
bootstrap resamples tasks, so it ignores that tasks of one family are correlated: intervals are
narrower than the truth. Read them as a floor on the uncertainty.
"""
import argparse
import json
import os
import random
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tasks  # noqa: E402


def load(path):
    """{(family, seed): [records]} for completed trials (runner errors are left out)."""
    by = {}
    with open(os.path.join(path, "results.jsonl"), encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("pass") is None:
                continue
            by.setdefault((r["family"], r["seed"]), []).append(r)
    return by


def per_task(by, field):
    """{(family, seed): mean of field over reps}; field is a key or a callable on a record."""
    get = field if callable(field) else (lambda r: r.get(field))
    out = {}
    for k, rs in by.items():
        vals = [get(r) for r in rs if get(r) is not None]
        if vals:
            out[k] = sum(vals) / len(vals)
    return out


def tokens(r):
    a, b = r.get("tokens_in"), r.get("tokens_out")
    return None if a is None or b is None else a + b


def same_reps(a, b):
    """Tasks present in both with the same number of reps: unequal reps mean one side lost
    trials (runner error, interrupted run) and comparing means would mix noise levels."""
    return [k for k in a if k in b and len(a[k]) == len(b[k])]


def in_splits(keys, splits):
    names = {f for s in splits for f in tasks.SPLITS[s]}
    return [k for k in keys if k[0] in names]


def boot_ci(diffs, n=4000, seed=0):
    rnd = random.Random(seed)
    m = len(diffs)
    means = sorted(sum(diffs[rnd.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def paired(a, b, field, splits):
    """mean(a) - mean(b) over shared tasks with a 95% bootstrap CI. a, b: loaded results."""
    pa, pb = per_task(a, field), per_task(b, field)
    ok = set(same_reps(a, b))
    keys = in_splits([k for k in pa if k in ok], splits)
    d = [pa[k] - pb[k] for k in keys]
    if not d:
        return None
    lo, hi = boot_ci(d)
    return {"n": len(d), "mean_a": sum(pa[k] for k in keys) / len(d),
            "mean_b": sum(pb[k] for k in keys) / len(d), "diff": sum(d) / len(d),
            "lo": lo, "hi": hi}


def token_ratio(a, b, splits):
    """mean(tokens_a)/mean(tokens_b) per task, as a paired log-ratio bootstrap on tasks."""
    import math
    pa, pb = per_task(a, tokens), per_task(b, tokens)
    ok = set(same_reps(a, b))
    keys = in_splits([k for k in pa if k in pb and k in ok and pa[k] > 0 and pb[k] > 0], splits)
    if not keys:
        return None
    d = [math.log(pa[k] / pb[k]) for k in keys]
    lo, hi = boot_ci(d)
    return {"ratio": math.exp(sum(d) / len(d)), "lo": math.exp(lo), "hi": math.exp(hi)}


def summary(by, splits):
    keys = in_splits(list(by), splits)
    rows = {}
    for fam in sorted({k[0] for k in keys}, key=lambda f: (tasks.FAMILIES[f]["split"], f)):
        rs = [r for k in keys if k[0] == fam for r in by[k]]
        rows[fam] = {"split": tasks.FAMILIES[fam]["split"], "trials": len(rs),
                     "pass": sum(r["pass"] for r in rs) / len(rs),
                     "harness_ok": sum(bool(r["harness_ok"]) for r in rs) / len(rs),
                     "tokens": statistics.mean(t for t in map(tokens, rs) if t is not None)
                     if any(tokens(r) is not None for r in rs) else None,
                     "seconds": statistics.mean(r["seconds"] for r in rs)}
    return rows


def show(by, splits):
    rows = summary(by, splits)
    print(f"{'family':<13}{'split':<7}{'n':>4}{'pass':>7}{'harn_ok':>9}{'tokens':>9}{'sec':>7}")
    for fam, r in rows.items():
        print(f"{fam:<13}{r['split']:<7}{r['trials']:>4}{r['pass']:>7.2f}{r['harness_ok']:>9.2f}"
              f"{(r['tokens'] or 0):>9.0f}{r['seconds']:>7.0f}")
    for sp in splits:
        rs = [r for k in in_splits(list(by), [sp]) for r in by[k]]
        if rs:
            print(f"{'ALL ' + sp:<20}{len(rs):>4}{sum(r['pass'] for r in rs) / len(rs):>7.3f}"
                  f"{sum(bool(r['harness_ok']) for r in rs) / len(rs):>9.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--vs")
    ap.add_argument("--splits", default="train,val,test")
    a = ap.parse_args()
    splits = a.splits.split(",")
    by = load(a.path)
    show(by, splits)
    if a.vs:
        base = load(a.vs)
        for sp in splits:
            p = paired(by, base, "pass", [sp])
            h = paired(by, base, lambda r: float(bool(r["harness_ok"])), [sp])
            t = token_ratio(by, base, [sp])
            if p:
                print(f"\n[{sp}] pass  {p['mean_a']:.3f} vs {p['mean_b']:.3f}  diff {p['diff']:+.3f} "
                      f"95%CI [{p['lo']:+.3f}, {p['hi']:+.3f}] over {p['n']} tasks")
            if h:
                print(f"[{sp}] harness_ok diff {h['diff']:+.3f}")
            if t:
                print(f"[{sp}] tokens ratio {t['ratio']:.3f} 95%CI [{t['lo']:.3f}, {t['hi']:.3f}]")


if __name__ == "__main__":
    main()
