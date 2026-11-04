"""Measuring how well the model's judgement matches labels. Pure functions, no I/O.

What is measured, fixed here before any data is seen (Slice 4, Gate 2):

* The unit of analysis is a labelled ROW. Repeating a run does not make more rows, so the
  intervals below are computed on the rows of ONE run (the first); repeats only describe stability.
* Intention to classify: every row that was sent to the model stays in the denominator. A row the
  model gave no usable answer for (`UNAVAILABLE`) is not a correct classification. A second block
  (`diagnostic`) looks at answered rows only; it is not the main result.
* Every proportion carries k, n and a Wilson interval. With n == 0 there is nothing to estimate
  and the estimate and interval are None (never 0 or 1).
* Nothing here decides that a model is good enough. `precision_of_estimate` only says whether the
  intervals are narrow enough for a target the operator chose.
"""
import math
from statistics import NormalDist

NULL = "null"                  # the model said: no rule applies
UNAVAILABLE = "unavailable"    # no usable answer (unreadable, left out, unknown rule, timed out)


def _z(confidence):
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
            or not math.isfinite(confidence) or not 0 < confidence < 1:
        raise ValueError("confidence must be a number strictly between 0 and 1")
    return NormalDist().inv_cdf(0.5 + confidence / 2)


def _interval(p, n, z):
    """Wilson score interval for a proportion p observed over n trials, clipped to [0, 1]."""
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    low, high = max(0.0, centre - half), min(1.0, centre + half)
    # the bound that touches 0 or 1 is exactly 0 or 1; rounding must not turn it into 2.8e-17
    return (0.0 if p == 0 else low), (1.0 if p == 1 else high)


def wilson(k, n, confidence=0.95):
    """-> (low, high), or None when n == 0."""
    for v in (k, n):
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("k and n must be whole numbers")
    if n < 0 or k < 0 or k > n:
        raise ValueError("need 0 <= k <= n")
    z = _z(confidence)
    if n == 0:
        return None
    return _interval(k / n, n, z)


def half_width(interval):
    return None if interval is None else (interval[1] - interval[0]) / 2


def rows_needed(k, n, target_half_width, confidence=0.95):
    """The smallest number of rows n' >= n for which the Wilson interval around the SAME observed
    proportion k/n would be at most `target_half_width` wide on each side.

    ASSUMES the rows are independent and the proportion does not change as rows are added. It is
    an order of magnitude, not a power analysis. None when n == 0."""
    z = _z(confidence)
    if isinstance(target_half_width, bool) or not isinstance(target_half_width, (int, float)) \
            or not math.isfinite(target_half_width) or not 0 < target_half_width <= 0.5:
        raise ValueError("target_half_width must be a number in (0, 0.5]")
    if wilson(k, n, confidence) is None:
        return None
    p = k / n

    def enough(m):
        return half_width(_interval(p, m, z)) <= target_half_width

    if enough(n):
        return n
    lo, hi = n, n * 2
    while not enough(hi):
        lo, hi = hi, hi * 2
        if hi > 10 ** 9:
            return None
    while hi - lo > 1:                       # the width only shrinks as rows are added
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if enough(mid) else (mid, hi)
    return hi


def proportion(k, n, confidence=0.95):
    iv = wilson(k, n, confidence)
    return {"k": k, "n": n, "estimate": None if n == 0 else k / n,
            "interval": None if iv is None else [iv[0], iv[1]]}


def _check_rows(rows, rules):
    known = set(rules)
    for r in rows:
        if r["label"] is not None and r["label"] not in known:
            raise ValueError(f"label {r['label']!r} is not one of the rules")
        if r["outcome"] not in (NULL, UNAVAILABLE) and r["outcome"] not in known:
            raise ValueError(f"outcome {r['outcome']!r} is neither null, unavailable nor one of the rules")
    return known


def _block(rows, rules, confidence, with_availability):
    known = _check_rows(rows, rules)
    n = len(rows)
    pos = [r for r in rows if r["label"] is not None]
    neg = [r for r in rows if r["label"] is None]
    pred = [r for r in rows if r["outcome"] in known]
    tp = [r for r in pos if r["outcome"] in known]
    tn = [r for r in neg if r["outcome"] == NULL]
    both = [r for r in pos if r["outcome"] in known]
    out = {
        "rows": n, "positives": len(pos), "negatives": len(neg), "predicted_positive": len(pred),
        "recall": proportion(len(tp), len(pos), confidence),
        "precision": proportion(len(tp), len(pred), confidence),
        "specificity": proportion(len(tn), len(neg), confidence),
        "exact_rule_accuracy": proportion(sum(1 for r in both if r["outcome"] == r["label"]), len(both),
                                          confidence),
    }
    if with_availability:
        out["availability"] = proportion(sum(1 for r in rows if r["outcome"] != UNAVAILABLE), n, confidence)
    per = {}
    for rule in sorted(known):
        labelled = [r for r in rows if r["label"] == rule]
        said = [r for r in rows if r["outcome"] == rule]
        tp_r = sum(1 for r in labelled if r["outcome"] == rule)
        per[rule] = {"labelled": len(labelled), "predicted": len(said),
                     "precision": proportion(tp_r, len(said), confidence),
                     "recall": proportion(tp_r, len(labelled), confidence)}
    tp_all = sum(p["recall"]["k"] for p in per.values())
    out["per_rule"] = per
    out["micro"] = {"precision": proportion(tp_all, sum(p["predicted"] for p in per.values()), confidence),
                    "recall": proportion(tp_all, sum(p["labelled"] for p in per.values()), confidence)}
    return out


def score(rows, rules, confidence=0.95):
    """rows: [{"label": None | rule, "outcome": NULL | UNAVAILABLE | rule}], the rows of ONE run
    (rows the labeller marked uncertain are not in it). -> {"primary": ..., "diagnostic": ...}.

    precision alone can be flattered by a model that declines to answer, so it must be read with
    recall (which counts declined positives as misses) and availability."""
    rows = list(rows)
    answered = [r for r in rows if r["outcome"] != UNAVAILABLE]
    return {"primary": _block(rows, rules, confidence, True),
            "diagnostic": _block(answered, rules, confidence, False)}


def precision_of_estimate(primary, target_half_width, confidence=0.95):
    """Is each headline proportion estimated as narrowly as the operator asked? Says nothing about
    whether the model is any good."""
    heads = {name: primary[name] for name in ("recall", "precision", "specificity", "availability",
                                              "exact_rule_accuracy") if name in primary}
    per, met, missed, empty = {}, [], [], []
    for name, p in heads.items():
        if p["n"] == 0:
            per[name] = {"k": 0, "n": 0, "half_width": None, "met": None, "rows_needed": None}
            empty.append(name)
            continue
        hw = half_width(p["interval"])
        ok = hw <= target_half_width
        per[name] = {"k": p["k"], "n": p["n"], "half_width": hw, "met": ok,
                     "rows_needed": rows_needed(p["k"], p["n"], target_half_width, confidence)}
        (met if ok else missed).append(name)
    return {
        "target_half_width": target_half_width, "confidence": confidence, "per_metric": per,
        "assumption": "rows_needed assumes the rows are independent and the observed proportion stays the same",
        "statement": [
            f"Precision of the estimates (target half-width +/-{target_half_width} at {confidence:.0%}): "
            f"met for {', '.join(met) or 'none'}; not met for {', '.join(missed) or 'none'}; "
            f"no data for {', '.join(empty) or 'none'}.",
            "Model quality: no acceptance threshold has been set; this report does not say the model is good enough.",
        ],
    }


def stability(runs):
    """runs[r][i] is the outcome of row i in repeat r. Descriptive only: repeats of the same rows are
    not independent samples, so no interval is given."""
    runs = [list(r) for r in runs]
    if len(runs) < 2:
        return {"repeats": len(runs), "available": False}
    n = len(runs[0])
    if any(len(r) != n for r in runs):
        raise ValueError("every repeat must cover the same rows")
    if n == 0:
        return {"repeats": len(runs), "available": True, "rows": 0, "fully_consistent_rows": 0,
                "fully_consistent_share": None, "mean_modal_share": None}
    consistent, shares = 0, []
    for i in range(n):
        column = [r[i] for r in runs]
        top = max(column.count(v) for v in set(column))
        shares.append(top / len(runs))
        consistent += top == len(runs)
    return {"repeats": len(runs), "available": True, "rows": n, "fully_consistent_rows": consistent,
            "fully_consistent_share": consistent / n, "mean_modal_share": sum(shares) / n}


def order_sensitivity(forward, reversed_, singleton, baseline=None, confidence=0.95):
    """Share of rows whose outcome changed when the order of their batch was reversed (same members).

    This is DESCRIPTIVE order sensitivity, not an identification of a position effect: reversing the
    order also changes which rows come before and after each row, and the model is not
    deterministic. `baseline` (a second run in the original order) shows how much the outcomes move
    between plain reruns; it is a noise reference, not a separation of the two. Rows in a batch of
    one are left out."""
    if not (len(forward) == len(reversed_) == len(singleton)):
        raise ValueError("forward, reversed and singleton must cover the same rows")
    if baseline is not None and len(baseline) != len(forward):
        raise ValueError("the baseline must cover the same rows")
    keep = [i for i, s in enumerate(singleton) if not s]
    changed = sum(1 for i in keep if forward[i] != reversed_[i])
    out = {"rows": len(keep), "changed": proportion(changed, len(keep), confidence),
           "baseline": None,
           "note": "descriptive order sensitivity above ordinary rerun variation; not a position effect"}
    if baseline is not None:
        b = sum(1 for i in keep if forward[i] != baseline[i])
        out["baseline"] = proportion(b, len(keep), confidence)
    return out
