"""A report (the JSON `eval` writes) as plain text a person can read in a terminal.

Everything here comes from the report, plus -- only if the caller passes the corpus file and asks for
`errors` -- the first words of the messages the model got wrong. That text is printed to the terminal,
never written into a report. Nothing here decides that a model is good or bad; the numbers are
printed as the report has them, and the report's own two closing sentences are printed last.
"""
from . import sanitize

SNIPPET = 70


def _pct(p):
    return "  -  " if p is None else f"{p * 100:5.1f}%"


def _line(label, m):
    if m["n"] == 0:
        return f"  {label:<24}{m['k']:>4}/{m['n']:<4}   無資料"
    lo, hi = m["interval"]
    return f"  {label:<24}{m['k']:>4}/{m['n']:<4} {_pct(m['estimate'])}  區間 [{lo * 100:.1f}%, {hi * 100:.1f}%]"


NAMES = (("recall", "召回 recall"), ("precision", "精確度 precision"), ("specificity", "特異度 specificity"),
         ("availability", "可用率 availability"), ("exact_rule_accuracy", "規則完全相符"))


def _wrong_rows(report):
    """Scored rows whose FIRST pass outcome is not the label (a missing answer counts as wrong)."""
    out = []
    for r in report["rows"]:
        if r["uncertain"] or r["truncated_chars"]:
            continue
        want = r["label"] if r["label"] is not None else "null"
        got = r["outcomes"][0]
        if got != want:
            out.append((r["id"], want, got))
    return out


def snippets(corpus_path, hash_ids=False):
    """{row id: first words of its text} from a corpus file; unreadable lines are skipped. With hash_ids
    the keys are the hashed ids a report made with --hash-ids contains."""
    from . import corpus
    import json
    found = {}
    with open(corpus_path, encoding="utf-8-sig") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("id"), str) and isinstance(obj.get("text"), str):
                flat = sanitize.visible(" ".join(obj["text"].split()))     # layout first, then spell the rest
                found[corpus.hashed_id(obj["id"]) if hash_ids else obj["id"]] = flat[:SNIPPET] + ("…" if len(flat) > SNIPPET else "")
    return found


def render(report, errors=False, texts=None):
    """-> str. `texts` is {row id: snippet} (from `snippets`) and only used with errors=True."""
    meta, corp = report["meta"], report["corpus"]
    out = []
    add = out.append
    add(f"模型 {sanitize.visible(str(meta['model']))}；規則 {', '.join(r['name'] for r in meta['rules'])}；"
        f"重複 {meta['repeats']} 次；順序檢查 {'有' if meta['order_check'] else '無'}")
    add(f"語料 {corp['rows']} 列：計分 {corp['scored']}（有規則 {corp['positives']}、無規則 {corp['negatives']}）、"
        f"不確定 {corp['uncertain']}、被截斷 {corp['truncated']}")
    add("")
    add(f"主要結果（只看第 1 次；分母含沒得到答案的列；{meta['confidence'] * 100:.0f}% Wilson 區間）")
    for key, label in NAMES:
        if key in report["primary"]:
            add(_line(label, report["primary"][key]))
    add("  → 召回的分母是標了規則的列；特異度的分母是標「無規則」的列；兩者的列數都要夠多。")
    per = report["primary"]["per_rule"]
    if per:
        add("")
        add("逐規則（標註數／模型說的次數）")
        for name, p in per.items():
            add(f"  {name:<20}標註 {p['labelled']:>3}／模型說 {p['predicted']:>3}   "
                f"precision {p['precision']['k']}/{p['precision']['n']}   recall {p['recall']['k']}/{p['recall']['n']}")
    diag = report["diagnostic"]
    add("")
    add("只看有得到答案的列（輔助，不是主要結果）")
    for key, label in NAMES[:3] + NAMES[4:]:
        if key in diag:
            add(_line(label, diag[key]))
    stab, order = report["stability"], report["order_sensitivity"]
    add("")
    if stab.get("available"):
        add(f"穩定度：{stab['fully_consistent_rows']}/{stab['rows']} 列在 {stab['repeats']} 次重複中答案完全一致"
            f"（描述性，重複不是獨立樣本）")
    else:
        add("穩定度：只跑了 1 次，沒有資料")
    if order:
        base = order["baseline"]
        add(f"順序敏感度：倒序批次後答案改變 {order['changed']['k']}/{order['changed']['n']} 列"
            + (f"；同序重跑的改變 {base['k']}/{base['n']} 列（雜訊參考）" if base else "；沒有同序重跑可比較（加 --repeats 2）")
            + "。描述性，不是位置效應的估計")
    f = report["failures"]
    bad = {k: v for k, v in f.items() if v}
    add("失敗：" + ("無" if not bad else "，".join(f"{k} {v}" for k, v in bad.items())))
    for w in report["warnings"]:
        add(f"警告：{w}")
    add("")
    add("結論（報告原文）")
    for st in report["conclusion"]["statement"]:
        add(f"  {st}")
    nm = {k: v for k, v in report["conclusion"]["per_metric"].items() if v["rows_needed"]}
    if nm:
        add("  要把半寬壓到 ±" + f"{report['conclusion']['target_half_width']}，各指標約需的列數（假設各列獨立、比例不變）："
            + "、".join(f"{k} {v['rows_needed']}" for k, v in nm.items()))
    if errors:
        wrong = _wrong_rows(report)
        add("")
        add(f"答錯或沒答案的計分列（第 1 次）：{len(wrong)}")
        for rid, want, got in wrong:
            add(f"  {sanitize.visible(rid)}  標註 {want}  模型 {got}"
                + (f"  | {texts[rid]}" if texts and rid in texts else ""))
    return "\n".join(out) + "\n"
