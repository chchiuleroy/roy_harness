"""Harness²-style improvement loop on AGENT.md / skills, with a Qwen proposer.

    python bench/optimize.py --base bench_runs/base --out bench_runs/opt --rounds 2

Each round: the proposer (the same vLLM model the harness runs on) reads a digest of TRAIN
failures and costly passes and writes two candidates, one about cross-task habits ("invariant")
and one about this environment ("environment"). Each candidate is run on VAL only; the
acceptance rule in README.md picks at most one. The proposer never sees val or test traces,
and a candidate that names a fixture file of any task is rejected before it is run.
The test split is not touched here.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src", "harness"))
import analyze  # noqa: E402
import tasks  # noqa: E402
import harness_vllm as hv  # noqa: E402

MAX_AGENT_MD = 4000
MAX_SKILLS = 2
KINDS = {
    "invariant": ("habits that apply to ANY task: planning, checking your own work, "
                  "reading the task statement and every file it points to, keeping side "
                  "effects small, how to report"),
    "environment": ("facts about THIS environment that the failures show: the host OS and shell, "
                    "text encodings, how the file tools behave, limits on tool output"),
}
# Words that only make sense for one task. A candidate containing one is a memorised answer.
FORBIDDEN = ["sales_jan", "sales_feb", "sales_mar", "service.log", "answer.txt", "report.txt",
             "config.ini", "calc.py", "util.py", "test_calc", "staff.csv", "dept_totals",
             "counts.json", "records.json", "top.json", "notes.txt", "summary.txt", "pwned",
             "zh.txt", "stats.json", "grades.json", "gpa.csv", "store.py", "changelog",
             "fetch_rows", "prefetch", "falcon", "readme.txt", "reconcile", "biglog",
             "cli.py", "report.py", "test_all", "counts"]

PROPOSER_SYSTEM = (
    "You write the instruction files of a command-line coding agent. The agent runs on "
    "Windows; its run_command tool uses cmd.exe; its other tools are read_file, write_file, "
    "edit_file, todo_write, agent (subagent), task_output, task_stop, ask_user, "
    "schedule_wakeup, monitor. AGENT.md is added to the system prompt of every request. A "
    "skill is a folder with SKILL.md (name, one-line description, body) that the agent loads "
    "on demand. You will see how the agent behaved on some tasks. Write guidance that would "
    "have prevented the failures AND helps on tasks you have not seen. Rules: no task-specific "
    "file names, numbers or answers; every line must be a general habit or a fact about the "
    "environment; short imperative sentences; do not repeat what the tool descriptions already "
    "say; AGENT.md at most 3500 characters; at most 2 skills. Behaviour listed under KEEP "
    "works today: your text must not change it. A command line may appear in your text only if "
    "the same command ran successfully in a trace above; never invent shell syntax. Drop any "
    "line that is not tied to evidence in the digest.")

FORMAT = (
    "Reply in exactly this format and nothing else:\n"
    "===RATIONALE===\n(2-4 sentences: which failures or costs this addresses)\n"
    "===AGENT.md===\n(the full new text of AGENT.md)\n"
    "===SKILL name=lowercase-name description=one line===\n(body)   <- optional, up to 2\n"
    "===END===")


def short(s, n):
    s = (s or "").replace("\r", "")
    return s if len(s) <= n else s[:n] + f"...[{len(s) - n} more chars]"


def trace_digest(trial_dir, limit=2200):
    try:
        with open(os.path.join(trial_dir, "session.json"), encoding="utf-8") as f:
            msgs = json.load(f)
    except (OSError, ValueError):
        return "(no trace)"
    names, out = {}, []
    for m in msgs[1:]:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                fn = c["function"]
                names[c["id"]] = fn["name"]
                out.append(f"CALL {fn['name']} {short(fn['arguments'], 220)}")
            if (m.get("content") or "").strip():
                out.append(f"SAY {short(m['content'], 240)}")
        elif m.get("role") == "tool":
            out.append(f"  -> {short(m.get('content'), 200)}")
    text = "\n".join(out)
    return text if len(text) <= limit else text[:limit // 2] + "\n...\n" + text[-limit // 2:]


def failure_mode(r):
    """Failures that share a task type and the same kind of verdict are one failure mode:
    digits and quoted values are masked so '3 files' and '5 files' fall together."""
    why = re.sub(r"\d+", "N", re.sub(r"['\"].*?['\"]", "'_'", r["why"] or ""))
    return r["family"], why[:60]


def digest(results_dir, by):
    recs = [r for k in analyze.in_splits(list(by), ["train"]) for r in by[k]]
    lines = ["Pass rate and mean tokens per task type:"]
    rows = analyze.summary(by, ["train"])
    for fam, row in rows.items():
        lines.append(f"- task type {fam}: pass {row['pass']:.2f}, tokens {row['tokens'] or 0:.0f}, "
                     f"seconds {row['seconds']:.0f}")
    fails = [r for r in recs if not r["pass"]]
    modes = {}
    for r in fails:                                   # weakness mining: one entry per failure mode
        modes.setdefault(failure_mode(r), []).append(r)
    ranked = sorted(modes.values(), key=len, reverse=True)[:6]
    for rs in ranked:
        r = rs[0]
        lines.append(f"\n### FAILURE MODE ({r['family']}; {len(rs)} of {len(fails)} failed runs)\n"
                     f"Task: {short(r['prompt'], 600)}\nVerdict: {short(r['why'], 300)}\nTrace of "
                     f"one such run:\n" + trace_digest(os.path.join(results_dir, "trials", r["trial"])))
    for fam, row in rows.items():                     # what already works must survive the edit
        if row["pass"] < 0.9:
            continue
        ok = [r for r in recs if r["family"] == fam and r["pass"] and r.get("model_calls")]
        if ok:
            r = min(ok, key=lambda r: r["model_calls"])
            lines.append(f"\n### KEEP ({fam}: passes {row['pass']:.2f}; do not change this "
                         f"behaviour)\nTrace of the shortest passing run:\n"
                         + trace_digest(os.path.join(results_dir, "trials", r["trial"]), 700))
    costly = sorted((r for r in recs if r["pass"] and r.get("tokens_in") is not None),
                    key=lambda r: -(r["tokens_in"] + (r["tokens_out"] or 0)))[:2]
    for r in costly:
        lines.append(f"\n### EXPENSIVE but correct run ({r['family']}, {r['model_calls']} model "
                     f"calls, {r['tokens_in'] + r['tokens_out']} tokens)\nTask: "
                     f"{short(r['prompt'], 400)}\nTrace:\n"
                     + trace_digest(os.path.join(results_dir, "trials", r["trial"]), 1400))
    return "\n".join(lines), len(fails)


def parse_candidate(text):
    m = re.search(r"===AGENT\.md===\n(.*?)(?=\n===(?:SKILL|END)|\Z)", text, re.S)
    if not m:
        return None
    cand = {"agent": m.group(1).strip(), "skills": []}
    r = re.search(r"===RATIONALE===\n(.*?)\n===AGENT", text, re.S)
    cand["rationale"] = r.group(1).strip() if r else ""
    for s in re.finditer(r"===SKILL name=([A-Za-z0-9._-]+) description=(.*?)===\n(.*?)(?=\n===(?:SKILL|END)|\Z)",
                         text, re.S):
        cand["skills"].append({"name": s.group(1), "description": " ".join(s.group(2).split()),
                               "body": s.group(3).strip()})
    return cand


def problems(cand):
    blob = (cand["agent"] + "\n" + "\n".join(s["description"] + s["body"] for s in cand["skills"]))
    bad = [w for w in FORBIDDEN if w in blob.lower()]
    out = [f"mentions task-specific word(s): {bad}"] if bad else []
    if len(cand["agent"]) > MAX_AGENT_MD:
        out.append(f"AGENT.md is {len(cand['agent'])} chars, limit {MAX_AGENT_MD}")
    if len(cand["skills"]) > MAX_SKILLS:
        out.append("too many skills")
    if not cand["agent"]:
        out.append("empty AGENT.md")
    return out


def write_candidate(cand, folder):
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    with open(os.path.join(folder, "AGENT.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(cand["agent"] + "\n")
    for s in cand["skills"]:
        os.makedirs(os.path.join(folder, "skills", s["name"]))
        with open(os.path.join(folder, "skills", s["name"], "SKILL.md"), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write(f"---\nname: {s['name']}\ndescription: {s['description']}\n---\n{s['body']}\n")


def propose(kind, current, dig, history, folder):
    cur = "(none yet: the agent currently has no AGENT.md and no skills)" if current is None \
        else current
    tried = "\n".join(history) or "(nothing yet)"
    note = ""
    for attempt in range(3):
        user = (f"Focus: {KINDS[kind]}.\n\nCurrent AGENT.md and skills:\n{cur}\n\n"
                f"Earlier attempts this session:\n{tried}\n\n=== BEHAVIOUR DIGEST ===\n{dig}\n\n"
                f"{note}{FORMAT}")
        reply = hv.call_llm([{"role": "user", "content": user}], [], PROPOSER_SYSTEM,
                            max_tokens=6000, temperature=0.7)
        text = reply["choices"][0]["message"].get("content") or ""
        cand = parse_candidate(text)
        if cand is None:
            note = "Your last reply did not follow the format. "
            continue
        bad = problems(cand)
        if not bad:
            write_candidate(cand, folder)
            with open(os.path.join(folder, "rationale.txt"), "w", encoding="utf-8") as f:
                f.write(cand["rationale"])
            return cand
        note = "Your last reply was rejected: " + "; ".join(bad) + ". Fix that. "
    return None


def describe(folder):
    if folder is None:
        return None
    parts = [open(os.path.join(folder, "AGENT.md"), encoding="utf-8").read()]
    sk = os.path.join(folder, "skills")
    if os.path.isdir(sk):
        for n in sorted(os.listdir(sk)):
            parts.append(f"[skill {n}]\n" + open(os.path.join(sk, n, "SKILL.md"), encoding="utf-8").read())
    return "\n\n".join(parts)


def run_bench(cand, splits, out, reps=3):
    cmd = [sys.executable, "-X", "utf8", os.path.join(HERE, "run_bench.py"), "--cand", cand,
           "--splits", splits, "--reps", str(reps), "--workers", "4", "--out", out]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)


GATED = ("val", "train")
COST_CEILING = 1.15            # a clearly dearer candidate must buy a real gain
GAIN_FOR_COST = 0.05


def no_regression(cand_by, base_by, splits):
    """Self-Harness validation: the held-out split AND the split the proposer studied must
    both be no worse, on answers and on the harness layer. -> (ok, facts)."""
    facts, ok = {}, True
    for sp in splits:
        p = analyze.paired(cand_by, base_by, "pass", [sp])
        h = analyze.paired(cand_by, base_by, lambda r: float(bool(r["harness_ok"])), [sp])
        want = len(tasks.task_list([sp]))
        if p is None or h is None or p["n"] != want or h["n"] != want:
            # a split that was not run, or lost tasks or reps (runner error, partial run),
            # cannot pass: pairing would silently shrink to whatever both sides still have
            return False, {**facts, f"{sp}_incomplete": True, f"{sp}_n": p and p["n"]}
        facts.update({f"{sp}_pass_diff": p["diff"], f"{sp}_harness_diff": h["diff"],
                      f"{sp}_n": p["n"]})
        ok = ok and p["diff"] >= 0 and h["diff"] >= 0
    return ok, facts


def decide(cand_by, base_by, splits=GATED):
    """README acceptance rule: no regression on every gated split, then a gain pooled over
    them (more passes, or clearly cheaper), and not clearly dearer without a real gain."""
    ok, facts = no_regression(cand_by, base_by, splits)
    if not ok:
        return False, facts
    p = analyze.paired(cand_by, base_by, "pass", list(splits))
    t = analyze.token_ratio(cand_by, base_by, list(splits))
    facts.update({"pass_diff": p["diff"], "pass_lo": p["lo"], "pass_hi": p["hi"],
                  "tokens_ratio": t and t["ratio"], "tokens_lo": t and t["lo"],
                  "tokens_hi": t and t["hi"], "n_tasks": p["n"]})
    cheaper = bool(t) and t["ratio"] <= 0.90 and t["hi"] < 1.0
    too_dear = bool(t) and t["lo"] > COST_CEILING and p["diff"] < GAIN_FOR_COST
    return (p["diff"] > 0 or cheaper) and not too_dear, facts


def evaluate(folder, base):
    """Run a candidate on val; only a val non-regression earns the (dearer) train run."""
    val_dir, train_dir = folder + "_val", folder + "_train"
    for d in (val_dir, train_dir):                  # resumable runner: stale results of an
        shutil.rmtree(d, ignore_errors=True)        # earlier candidate must not be reused
    run_bench(folder, "val", val_dir)
    base_by = analyze.load(base)
    cand_by = analyze.load(val_dir)
    ok, facts = no_regression(cand_by, base_by, ("val",))
    if not ok:
        return False, facts, val_dir, None
    run_bench(folder, "train", train_dir)
    for k, v in analyze.load(train_dir).items():
        cand_by[k] = v
    ok, facts = decide(cand_by, base_by)
    return ok, facts, val_dir, train_dir


def merge_results(dirs, out):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "results.jsonl"), "w", encoding="utf-8") as w:
        for d in dirs:
            with open(os.path.join(d, "results.jsonl"), encoding="utf-8") as f:
                w.write(f.read())
            shutil.copytree(os.path.join(d, "trials"), os.path.join(out, "trials"),
                            dirs_exist_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="results folder with train and val of the bare harness")
    ap.add_argument("--out", default="bench_runs/opt")
    ap.add_argument("--rounds", type=int, default=2)
    a = ap.parse_args()
    a.base = os.path.abspath(a.base)
    a.out = os.path.abspath(a.out)
    os.makedirs(a.out, exist_ok=True)
    best_dir, best_res = None, a.base               # folder with AGENT.md/skills; its result dir
    history, log = [], []
    for rnd in range(1, a.rounds + 1):
        dig, nfail = digest(best_res, analyze.load(best_res))
        print(f"round {rnd}: {nfail} failed train trials in the digest source", flush=True)
        with open(os.path.join(a.out, f"digest_r{rnd}.txt"), "w", encoding="utf-8") as f:
            f.write(dig)
        winners = []
        for kind in KINDS:
            folder = os.path.join(a.out, f"r{rnd}_{kind}")
            cand = propose(kind, describe(best_dir), dig, history, folder)
            if cand is None:
                history.append(f"round {rnd} {kind}: proposer failed to produce a valid candidate")
                log.append({"round": rnd, "kind": kind, "accepted": False, "reason": "invalid"})
                continue
            ok, facts, val_dir, train_dir = evaluate(folder, best_res)
            log.append({"round": rnd, "kind": kind, "accepted": ok, **facts})
            # the proposer is told the outcome and, for the split it can already see, the
            # reason; it is not told how the held-out split moved
            note = "ACCEPTED" if ok else (
                "rejected: worse on the training tasks" if facts.get("train_pass_diff", 0) < 0
                or facts.get("train_harness_diff", 0) < 0 else "rejected on the held-out check")
            history.append(f"round {rnd} {kind}: {note}; idea: {short(cand['rationale'], 200)}")
            print(f"  {kind}: {'ACCEPT' if ok else 'reject'} {facts}", flush=True)
            if ok:
                # rounded: 1/54 computed two ways differs in the 17th digit and decided a tie
                winners.append((round(facts["pass_diff"], 6), -(facts["tokens_ratio"] or 1),
                                folder, val_dir, train_dir))
        if not winners:
            print("no candidate accepted this round", flush=True)
            continue                                # the plan is T rounds, not "until the first miss"
        winners.sort(reverse=True)
        best_dir = winners[0][2]
        best_res = best_dir + "_all"                # val + train of the winner: next base and digest
        merge_results(winners[0][3:], best_res)
    with open(os.path.join(a.out, "log.json"), "w", encoding="utf-8") as f:
        json.dump({"best": best_dir, "log": log}, f, indent=1)
    print("best candidate:", best_dir)


if __name__ == "__main__":
    main()
