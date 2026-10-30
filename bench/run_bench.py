"""Run the benchmark: every (family, seed, rep) is one real harness process in its own folder.

    python bench/run_bench.py --cand none --splits train,val --reps 3 --out bench_runs/base
    python bench/run_bench.py --cand bench_runs/cand1 --splits val --reps 3 --out bench_runs/c1_val

`--cand` is a folder holding AGENT.md and/or skills/ (what an optimizer may edit), or `none`
for the bare harness. The harness code itself is never touched, so the permission gate, hooks
and crash recovery stay outside whatever is being optimized.

Per trial the output folder keeps: stdout.txt (the terminal), session.json (every message and
tool result), and one line in results.jsonl. Re-running with the same --out skips trials already
done, so a long run can be stopped and resumed.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tasks  # noqa: E402

HARNESS = os.path.join(HERE, "..", "src", "harness", "harness_vllm.py")
TRIAL_TIMEOUT = 300
LOCK = threading.Lock()
STAT_KEYS = ("model_calls", "tokens_in", "tokens_out", "tool_calls", "tool_errors",
             "gate_blocked", "denied_by_user", "asked_human", "llm_retries", "compactions")


def kill_tree(proc):
    if sys.platform == "win32":                    # terminate() leaves the children running
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()


def last_assistant(session):
    try:
        with open(session, encoding="utf-8") as f:
            msgs = json.load(f)
    except (OSError, ValueError):
        return ""
    for m in reversed(msgs):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            return m["content"]
    return ""


def parse_stats(text):
    hit = [l for l in text.splitlines() if l.startswith("[stats] ")]
    try:
        return json.loads(hit[-1][len("[stats] "):]) if hit else {}
    except ValueError:
        return {}


def run_trial(family, seed, rep, cand, out):
    assert os.path.isabs(out), "out must be absolute: the harness runs in a different cwd"
    fam = tasks.FAMILIES[family]
    tid = f"{family}-{seed}-r{rep}"
    tdir = os.path.join(out, "trials", tid)
    os.makedirs(tdir, exist_ok=True)
    work = tempfile.mkdtemp(prefix=f"bench_{tid}_")
    session = os.path.join(tdir, "session.json")
    if os.path.exists(session):
        os.remove(session)
    t0 = time.time()
    try:
        prompt, ctx = fam["make"](seed, work)
        if cand != "none":
            for name in tasks.HARNESS_FILES:
                src = os.path.join(cand, name)
                if os.path.isdir(src):
                    shutil.copytree(src, os.path.join(work, name))
                elif os.path.isfile(src):
                    shutil.copy(src, os.path.join(work, name))
        env = dict(os.environ, HARNESS_EOF_GRACE="3", PYTHONUNBUFFERED="1",
                   PYTHONIOENCODING="utf-8")
        stdout_path = os.path.join(tdir, "stdout.txt")
        timed_out = False
        with open(stdout_path, "wb") as so:
            proc = subprocess.Popen([sys.executable, "-X", "utf8", HARNESS, session], cwd=work,
                                    env=env, stdin=subprocess.PIPE, stdout=so,
                                    stderr=subprocess.STDOUT)
            try:
                proc.communicate((prompt + "\n").encode("utf-8"), timeout=TRIAL_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_tree(proc)
                proc.communicate()
        with open(stdout_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        final = last_assistant(session)
        stats = parse_stats(text)
        harness_ok = (not timed_out and proc.returncode == 0 and "[session saved]" in text
                      and "model call failed" not in text and "hit MAX_ROUNDS" not in text)
        ok, why = fam["verify"](work, final, ctx)
        rec = {"trial": tid, "family": family, "split": fam["split"], "seed": seed, "rep": rep,
               "pass": bool(ok), "why": why, "harness_ok": harness_ok, "timed_out": timed_out,
               "exit": proc.returncode, "seconds": round(time.time() - t0, 1),
               "final_tail": final[-300:], "prompt": prompt,
               **{k: stats.get(k) for k in STAT_KEYS}}
        with open(os.path.join(tdir, "workdir_listing.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(tasks.snapshot(work))))
        after = os.path.join(tdir, "work_after")     # keep the outputs: a verifier fix can re-grade
        shutil.rmtree(after, ignore_errors=True)
        for rel in tasks.snapshot(work):
            src = os.path.join(work, rel)
            if os.path.getsize(src) <= 100_000:
                os.makedirs(os.path.dirname(os.path.join(after, rel)), exist_ok=True)
                shutil.copy(src, os.path.join(after, rel))
    except Exception as e:                         # a benchmark bug must not look like a fail
        rec = {"trial": tid, "family": family, "split": fam["split"], "seed": seed, "rep": rep,
               "pass": None, "why": f"RUNNER ERROR {e!r}", "harness_ok": None,
               "seconds": round(time.time() - t0, 1)}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    with LOCK:
        with open(os.path.join(out, "results.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def done_keys(out):
    path = os.path.join(out, "results.jsonl")
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {r["trial"] for r in map(json.loads, f) if r.get("pass") is not None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cand", default="none")
    ap.add_argument("--splits", default="train,val")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seeds", choices=["default", "fresh"], default="default",
                    help="fresh = tasks.FRESH_SEEDS, instances no earlier run has seen")
    ap.add_argument("--families", default="", help="comma list to restrict (debugging)")
    a = ap.parse_args()
    a.out = os.path.abspath(a.out)       # the harness runs in another cwd: a relative session path
    os.makedirs(a.out, exist_ok=True)    # silently fails to save there (found the hard way)
    if a.cand != "none":
        a.cand = os.path.abspath(a.cand)
    todo = [(f, s, r) for (f, s) in tasks.task_list(a.splits.split(","),
                                     tasks.FRESH_SEEDS if a.seeds == "fresh" else None)
            for r in range(a.reps) if not a.families or f in a.families.split(",")]
    skip = done_keys(a.out)
    todo = [t for t in todo if f"{t[0]}-{t[1]}-r{t[2]}" not in skip]
    with open(os.path.join(a.out, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"cand": a.cand, "splits": a.splits, "reps": a.reps, "workers": a.workers,
                   "started": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    print(f"{len(todo)} trials to run ({len(skip)} already done) -> {a.out}", flush=True)
    n = [0]

    def go(t):
        rec = run_trial(*t, a.cand, a.out)
        with LOCK:
            n[0] += 1
            flag = {True: "PASS", False: "fail", None: "ERR "}[rec["pass"]]
            print(f"[{n[0]}/{len(todo)}] {flag} {rec['trial']:<22} {rec.get('seconds')}s "
                  f"{(rec.get('why') or '')[:70]}", flush=True)
    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(go, todo))


if __name__ == "__main__":
    main()
