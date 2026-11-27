"""Build a labelled corpus for `superagent eval` out of public GitHub issues, with two blind annotators.

Standard library only. All data lives in a work folder (--work-dir) OUTSIDE this repository: the issue texts are
other people's, so they are never committed. The stages, in order:

  fetch        draw issues at random, seeded, into candidates.jsonl (+ .sha256)         needs `gh`
  fetch-usage  optional second draw: issues the maintainers labelled usage/question      needs `gh`
  blind        cut a pool into annot/chunkN.jsonl holding only {id, text}; write mapping*.json
  prompts      write prompts/codex_chunkN.md (the annotation instructions + the chunk's file name)
  (annotate)   NOT in this script: two independent annotators fill results_codex/outN.md (last ```json block)
               and results_claude/chunkN.json; see README.md
  merge        check both annotators, write labelled_agreed.jsonl, disputed.jsonl, the two single-annotator
               corpora and agreement.json

Why the split: fetch depends on what GitHub holds today, so it is not repeatable; once candidates.jsonl is saved
(its sha256 is printed and stored), every later stage is deterministic and can be re-run from the files alone.
"""
import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys

DEFAULT_RULES = ("bug-report", "usage-question")
PROMPT_SUFFIX = "\n\n## 你被指定的檔案\n\n`{name}`（在目前工作目錄）。只讀這一個檔。\n"


# ---------------------------------------------------------------- small helpers
def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_jsonl(path, rows):
    write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run_gh(args):
    """-> stdout of `gh <args>`; exits with the error text if gh fails. Replaced by a fake in the tests."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:3])}: {r.stderr[:300]}")
    return r.stdout


def issue_text(item, cap):
    body = (item.get("body") or "").replace("\r\n", "\n").strip()
    return body, (item["title"].strip() + "\n\n" + body)[:cap]


def parse_paginated(buf):
    """`gh api --paginate` prints one JSON array per page, back to back."""
    dec, pos, items = json.JSONDecoder(), 0, []
    while pos < len(buf):
        while pos < len(buf) and buf[pos].isspace():
            pos += 1
        if pos >= len(buf):
            break
        obj, pos = dec.raw_decode(buf, pos)
        items.extend(obj)
    return items


def ids_from(path):
    return {row["id"] for row in read_jsonl(path)} if path else set()


# ---------------------------------------------------------------- stage: fetch
def fetch_issues(repos, since, cap, seed, exclude, gh=run_gh):
    """repos: [(owner/name, quota)]. Real issues only: no pull requests, no bots, a body of at least 80 characters,
    created on or after `since`, not in `exclude`. Seeded, so the same GitHub state gives the same draw."""
    rng = random.Random(seed)
    rows = []
    for repo, want in repos:
        items = parse_paginated(gh(["api", "--paginate", f"repos/{repo}/issues?state=all&per_page=100&since={since}"]))
        pool = []
        for it in items:
            if it.get("pull_request") is not None or (it.get("user") or {}).get("type") == "Bot":
                continue
            if it["created_at"] < since:
                continue
            body, text = issue_text(it, cap)
            rid = f"{repo.split('/')[1]}-{it['number']}"
            if len(body) < 80 or rid in exclude:
                continue
            pool.append({"id": rid, "text": text, "repo": repo, "number": it["number"], "url": it["html_url"],
                         "gh_labels": sorted(lb["name"] for lb in it.get("labels", []))})
        rng.shuffle(pool)
        print(repo, "pool", len(pool), "take", min(want, len(pool)))
        rows.extend(pool[:want])
    rng.shuffle(rows)
    return rows


def fetch_usage(plan, cap, seed, seen, carry_rows, carry_repo, carry_stratum, gh=run_gh):
    """A stratified, NOT random, second draw: issues a maintainer filed under a usage/question label (so the
    usage rule has enough rows to be measured), plus rows carried over from an earlier trial.
    plan: [(repo, label, since, want)]. Annotators never see the label that selected a row."""
    rng = random.Random(seed)
    rows, seen = [], set(seen)
    for repo, label, since, want in plan:
        q = f'repo:{repo} is:issue label:"{label}" created:>{since}'
        out = gh(["api", "-X", "GET", "search/issues", "-f", f"q={q}", "-f", "per_page=100", "--jq",
                  ".items|map({number,title,body,html_url,created_at,user:(.user.type),labels:[.labels[].name]})"])
        pool = []
        for it in json.loads(out):
            body, text = issue_text(it, cap)
            rid = f"{repo.split('/')[1]}-{it['number']}"
            if it["user"] == "Bot" or len(body) < 80 or rid in seen:
                continue
            pool.append({"id": rid, "text": text, "repo": repo, "number": it["number"], "url": it["html_url"],
                         "gh_labels": sorted(it["labels"]), "stratum": "usage-labelled"})
        rng.shuffle(pool)
        print(repo, "pool", len(pool), "take", min(want, len(pool)))
        for p in pool[:want]:
            seen.add(p["id"])
            rows.append(p)
    for o in carry_rows:
        if o["id"] in seen:
            continue
        seen.add(o["id"])
        rows.append({"id": o["id"], "text": o["text"].replace("\r\n", "\n")[:cap], "repo": carry_repo,
                     "number": int(o["id"].split("-")[1]), "url": "", "gh_labels": [], "stratum": carry_stratum,
                     "old_label": o["label"]})
    rng.shuffle(rows)
    return rows


# ---------------------------------------------------------------- stage: blind
def make_blind(rows, work, chunk=60, id_start=0, first_chunk=1, mapping_name="mapping.json", force=False):
    """Annotators get an opaque id and the text: no repo, number, url, GitHub labels. The way back
    (cNNNN -> real id) is the mapping file, which stays outside the annotators' folder. -> mapping dict.
    A file that already exists with different content is NOT overwritten (a wrong --id-start or --first-chunk
    would rewrite what an annotator already received); re-running with the same data is fine, --force overrides."""
    mapping, files = {}, {}
    for k in range(0, len(rows), chunk):
        part = rows[k:k + chunk]
        lines = []
        for i, row in enumerate(part, start=id_start + k + 1):
            cid = f"c{i:04d}"
            mapping[cid] = row["id"]
            lines.append({"id": cid, "text": row["text"]})
        files[os.path.join(work, "annot", f"chunk{first_chunk + k // chunk}.jsonl")] = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines)
    files[os.path.join(work, mapping_name)] = json.dumps(mapping, indent=0)
    if not force:
        for path, text in files.items():
            if os.path.exists(path):
                with open(path, encoding="utf-8", newline="") as f:
                    if f.read().replace("\r\n", "\n") != text:
                        raise SystemExit(f"{path} already exists with different content; check --id-start/--first-chunk "
                                         f"(or pass --force to overwrite)")
    for path, text in files.items():
        write_text(path, text)
    return mapping


def write_prompts(work, prompt_path):
    base = open(prompt_path, encoding="utf-8", newline="").read()
    names = sorted((f for f in os.listdir(os.path.join(work, "annot")) if re.fullmatch(r"chunk\d+\.jsonl", f)),
                   key=lambda f: int(re.findall(r"\d+", f)[0]))
    for name in names:
        n = re.findall(r"\d+", name)[0]
        write_text(os.path.join(work, "prompts", f"codex_chunk{n}.md"), base + PROMPT_SUFFIX.format(name=name))
    return names


# ---------------------------------------------------------------- stage: merge
def kappa(pairs):
    n = len(pairs)
    cats = sorted({p for pr in pairs for p in pr}, key=str)
    po = sum(1 for x, y in pairs if x == y) / n
    pe = sum((sum(1 for x, _ in pairs if x == c) / n) * (sum(1 for _, y in pairs if y == c) / n) for c in cats)
    return po, (po - pe) / (1 - pe) if pe < 1 else float("nan")


def dist(rows):
    out = {}
    for r in rows:
        out[str(r["label"])] = out.get(str(r["label"]), 0) + 1
    return out


def load_codex(work, n):
    txt = open(os.path.join(work, "results_codex", f"out{n}.md"), encoding="utf-8").read()
    blocks = re.findall(r"```json\s*(\[.*?\])\s*```", txt, flags=re.S)
    if not blocks:
        raise SystemExit(f"codex chunk{n}: no json block")
    return json.loads(blocks[-1])


def load_claude(work, n):
    with open(os.path.join(work, "results_claude", f"chunk{n}.json"), encoding="utf-8") as f:
        return json.load(f)


def check_items(work, items, n, who, allowed):
    want = [r["id"] for r in read_jsonl(os.path.join(work, "annot", f"chunk{n}.jsonl"))]
    got = [x["id"] for x in items]
    if got != want:
        raise SystemExit(f"{who} chunk{n}: ids differ (got {len(got)}, want {len(want)})")
    for x in items:
        if x["label"] not in allowed or not isinstance(x.get("uncertain"), bool):
            raise SystemExit(f"{who} chunk{n}: bad item {x}")


def merge(work, pools, mapping_names, rules=DEFAULT_RULES):
    """pools: [(candidates file name, default stratum or None)]. Writes the corpora and agreement.json into `work`.
    Rows both annotators labelled the same, neither marked uncertain, go to labelled_agreed.jsonl (the scored
    corpus); the rest to disputed.jsonl (not scored). Cohen's kappa is descriptive only (skewed classes)."""
    allowed = set(rules) | {None}
    mapping = {}
    for name in mapping_names:
        with open(os.path.join(work, name), encoding="utf-8") as f:
            part = json.load(f)
        clash = sorted(set(part) & set(mapping))
        if clash:      # two batches numbered the same ids: the later one would silently replace the earlier
            raise SystemExit(f"{name}: blind ids already used by an earlier mapping, e.g. {clash[:3]} (check --id-start)")
        mapping.update(part)
    if len(set(mapping.values())) != len(mapping):
        raise SystemExit("the mapping sends two blind ids to the same issue")
    cands = {}
    for fn, stratum in pools:
        for d in read_jsonl(os.path.join(work, fn)):
            d.setdefault("stratum", stratum)
            cands[d["id"]] = d
    missing = sorted(set(mapping.values()) - set(cands))
    if missing:
        raise SystemExit(f"the mapping names issues that are in no candidate pool, e.g. {missing[:3]}")
    chunks = sorted(int(re.findall(r"\d+", f)[0]) for f in os.listdir(os.path.join(work, "annot"))
                    if re.fullmatch(r"chunk\d+\.jsonl", f))
    A, B, seen_text = {}, {}, {}
    for n in chunks:
        a, b = load_codex(work, n), load_claude(work, n)
        check_items(work, a, n, "codex", allowed)
        check_items(work, b, n, "claude", allowed)
        for who, items, store in (("codex", a, A), ("claude", b, B)):
            for x in items:
                if x["id"] in store:
                    raise SystemExit(f"{who}: blind id {x['id']} appears in more than one chunk")
                store[x["id"]] = x
        for r in read_jsonl(os.path.join(work, "annot", f"chunk{n}.jsonl")):
            seen_text[r["id"]] = r["text"]
    ids = list(mapping)
    if not (set(A) == set(B) == set(ids)):
        raise SystemExit("id sets differ between the annotators and the mapping")
    for i in ids:    # what the annotators read must be what the mapping says it is
        if seen_text[i] != cands[mapping[i]]["text"]:
            raise SystemExit(f"{i}: the text the annotators were given differs from the candidate {mapping[i]}")

    pairs3 = [(A[i]["label"], B[i]["label"]) for i in ids]
    pairs2 = [(a is not None, b is not None) for a, b in pairs3]
    po3, k3 = kappa(pairs3)
    po2, k2 = kappa(pairs2)

    def row(i, label, **extra):
        c = cands[mapping[i]]
        d = {"id": c["id"], "text": c["text"], "label": label, "sender": "annotated", "thread": c["id"], "note": c["stratum"]}
        d.update(extra)
        return d

    agreed, disputed = [], []
    for i in ids:
        a, b = A[i], B[i]
        if a["label"] == b["label"] and not a["uncertain"] and not b["uncertain"]:
            agreed.append(row(i, a["label"]))
        else:
            disputed.append({"id": mapping[i], "codex": a["label"], "codex_uncertain": a["uncertain"],
                             "claude": b["label"], "claude_uncertain": b["uncertain"],
                             "gh_labels": cands[mapping[i]]["gh_labels"], "url": cands[mapping[i]]["url"]})
    write_jsonl(os.path.join(work, "labelled_agreed.jsonl"), agreed)
    write_jsonl(os.path.join(work, "disputed.jsonl"), disputed)
    write_jsonl(os.path.join(work, "labelled_codex_all.jsonl"), [row(i, A[i]["label"], uncertain=A[i]["uncertain"]) for i in ids])
    write_jsonl(os.path.join(work, "labelled_claude_all.jsonl"), [row(i, B[i]["label"], uncertain=B[i]["uncertain"]) for i in ids])

    by_repo = {}
    for r in agreed:
        by_repo.setdefault(cands[r["id"]]["repo"], []).append(r["label"])
    strata = sorted({c["stratum"] for c in cands.values()})
    report = {
        "rows": len(ids), "agreed": len(agreed), "disputed": len(disputed),
        "raw_agreement_3class": round(po3, 4), "kappa_3class": round(k3, 4),
        "raw_agreement_any_rule_vs_none": round(po2, 4), "kappa_any_rule_vs_none": round(k2, 4),
        "codex_uncertain": sum(A[i]["uncertain"] for i in ids), "claude_uncertain": sum(B[i]["uncertain"] for i in ids),
        "agreed_distribution": dist(agreed),
        "agreement_by_stratum": {st: {"rows": sum(1 for i in ids if cands[mapping[i]]["stratum"] == st),
                                       "same_label": sum(1 for i in ids if cands[mapping[i]]["stratum"] == st and A[i]["label"] == B[i]["label"]),
                                       "agreed_clean": sum(1 for r in agreed if r["note"] == st)}
                                 for st in strata},
        "agreed_distribution_by_stratum": {st: dist([r for r in agreed if r["note"] == st]) for st in strata},
        "codex_distribution": dist([{"label": A[i]["label"]} for i in ids]),
        "claude_distribution": dist([{"label": B[i]["label"]} for i in ids]),
        "agreed_by_repo": {k: {"rows": len(v), "none": sum(1 for x in v if x is None)} for k, v in by_repo.items()},
    }
    write_text(os.path.join(work, "agreement.json"), json.dumps(report, ensure_ascii=False, indent=1))
    return report


# ---------------------------------------------------------------- command line
def parse_quota(spec):
    out = []
    for part in spec.split(","):
        repo, _, n = part.rpartition("=")
        out.append((repo, int(n)))
    return out


def parse_plan(spec):
    out = []
    for part in spec.split(","):
        repo, rest = part.split(":", 1)
        label, since, n = rest.rsplit(":", 2)
        out.append((repo, label, since, int(n)))
    return out


def parse_pool(spec):
    name, _, stratum = spec.partition(":")
    return name, (stratum or None)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--work-dir", required=True, help="folder for all data (outside the repository)")

    p = sub.add_parser("fetch")
    common(p)
    p.add_argument("--repos", required=True, help="owner/name=quota,owner/name=quota")
    p.add_argument("--since", required=True, help="ISO time, e.g. 2026-09-15T00:00:00Z")
    p.add_argument("--cap", type=int, default=700)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--exclude-ids", help="jsonl with an id per row to leave out (an earlier trial)")
    p.add_argument("--out", default="candidates.jsonl")

    p = sub.add_parser("fetch-usage")
    common(p)
    p.add_argument("--plan", required=True, help="repo:label:since:N,... (label may contain spaces)")
    p.add_argument("--seen", default="candidates.jsonl", help="pool in work dir whose ids must not repeat")
    p.add_argument("--cap", type=int, default=700)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--carry", help="jsonl of rows (id, text, label) from an earlier trial to re-annotate blind")
    p.add_argument("--carry-repo")
    p.add_argument("--carry-stratum", default="carried")
    p.add_argument("--out", default="candidates2.jsonl")

    p = sub.add_parser("blind")
    common(p)
    p.add_argument("--pool", default="candidates.jsonl")
    p.add_argument("--chunk", type=int, default=60)
    p.add_argument("--id-start", type=int, default=0, help="rows before this pool (second pool: size of the first)")
    p.add_argument("--first-chunk", type=int, default=1)
    p.add_argument("--mapping", default="mapping.json")
    p.add_argument("--force", action="store_true", help="overwrite files that already exist with different content")

    p = sub.add_parser("prompts")
    common(p)
    p.add_argument("--prompt", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "annot_prompt.md"))

    p = sub.add_parser("merge")
    common(p)
    p.add_argument("--pool", action="append", help="FILE[:default stratum]; repeat; default candidates.jsonl:random-draw")
    p.add_argument("--mapping", action="append", help="mapping file name; repeat; default every mapping*.json")
    p.add_argument("--rules", default=",".join(DEFAULT_RULES))

    a = ap.parse_args(argv)
    work = a.work_dir
    if a.cmd == "fetch":
        rows = fetch_issues(parse_quota(a.repos), a.since, a.cap, a.seed, ids_from(a.exclude_ids), gh=run_gh)
        path = os.path.join(work, a.out)
        write_jsonl(path, rows)
        write_text(path + ".sha256", sha256_file(path) + "\n")
        print("wrote", len(rows), path, "sha256", sha256_file(path))
    elif a.cmd == "fetch-usage":
        carry = read_jsonl(a.carry) if a.carry else []
        if carry and not a.carry_repo:
            raise SystemExit("--carry needs --carry-repo")
        seen = {r["id"] for r in read_jsonl(os.path.join(work, a.seen))}
        rows = fetch_usage(parse_plan(a.plan), a.cap, a.seed, seen, carry, a.carry_repo, a.carry_stratum, gh=run_gh)
        path = os.path.join(work, a.out)
        write_jsonl(path, rows)
        write_text(path + ".sha256", sha256_file(path) + "\n")
        print("wrote", len(rows), path, "sha256", sha256_file(path))
    elif a.cmd == "blind":
        rows = read_jsonl(os.path.join(work, a.pool))
        mapping = make_blind(rows, work, a.chunk, a.id_start, a.first_chunk, a.mapping, a.force)
        print(len(mapping), "rows in", -(-len(rows) // a.chunk), "chunks")
    elif a.cmd == "prompts":
        print("wrote prompts for", write_prompts(work, a.prompt))
    elif a.cmd == "merge":
        pools = [parse_pool(s) for s in (a.pool or ["candidates.jsonl:random-draw"])]
        names = a.mapping or sorted((f for f in os.listdir(work) if re.fullmatch(r"mapping\d*\.json", f)),
                                    key=lambda f: int(re.findall(r"\d+", f)[0]) if re.findall(r"\d+", f) else 0)
        report = merge(work, pools, names, tuple(r for r in a.rules.split(",") if r))
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
