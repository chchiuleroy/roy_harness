"""Mutation check: break the code on purpose in an isolated copy and require the test suite to go red.
A mutant that survives means a test does not guard the behaviour it claims to guard.

  python tools/mutate.py                 run all mutations in tools/mutations.py (4 at a time)
  python tools/mutate.py -j 2 NAME ...   run selected ones with 2 workers

Each mutation is (name, relative file under src/kb or tools/, old text, new text); the old text must occur exactly once.
"""
import concurrent.futures as cf
import os
import shutil
import subprocess
import sys
import tempfile

TIMEOUT_S = 600
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from mutations import MUTATIONS  # noqa: E402


def one(m, timeout=TIMEOUT_S):
    name, rel, old, new = m
    work = tempfile.mkdtemp(prefix="kb_mut_")
    try:
        ignore = shutil.ignore_patterns("__pycache__")
        for d in ("src", "tests", "tools"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(work, d), ignore=ignore)
        for f in ("run_tests.py", "pyproject.toml"):
            shutil.copy(os.path.join(ROOT, f), work)
        path = os.path.join(work, rel) if rel.startswith("tools/") else os.path.join(work, "src", "kb", rel)
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        if text.count(old) != 1:
            return name, "error", f"old text occurs {text.count(old)} times in {rel}"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old, new))
        env = {k: v for k, v in os.environ.items() if k != "KB_FAULT"}
        try:
            r = subprocess.run([sys.executable, "run_tests.py"], cwd=work, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return name, "timeout", ""                    # NO conclusion: a sleeping laptop or a busy machine looks the same as a hang
        return name, ("killed" if r.returncode != 0 else "survived"), ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv):
    jobs = 4
    if "-j" in argv:
        i = argv.index("-j")
        jobs = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    selected = set(argv)
    todo = [m for m in MUTATIONS if not selected or m[0] in selected]
    results = []
    with cf.ThreadPoolExecutor(jobs) as ex:
        for res in ex.map(one, todo):
            results.append(res)
            print(f"[{len(results)}/{len(todo)}] {res[0]}: {res[1]}{' (' + res[2] + ')' if res[2] and res[1] != 'error' else ''}", flush=True)
    # A timeout proves nothing (the machine may have slept or been busy). Run each one again, alone, with twice the time; only that result counts.
    by_name = {m[0]: m for m in todo}
    for i, r in enumerate(results):
        if r[1] == "timeout":
            print(f"re-running alone after a timeout: {r[0]}", flush=True)
            results[i] = one(by_name[r[0]], TIMEOUT_S * 2)
            print(f"   {results[i][0]}: {results[i][1]}", flush=True)
    inconclusive = [r for r in results if r[1] == "timeout"]
    killed = [r for r in results if r[1] == "killed"]
    survived = [r for r in results if r[1] == "survived"]
    errors = [r for r in results if r[1] == "error"]
    print(f"killed {len(killed)}, survived {len(survived)}, could not apply {len(errors)}")
    for n, _, _ in survived:
        print("  SURVIVED:", n)
    for n, _, why in errors:
        print("  ERROR:", n, why)
    for n, _, _ in inconclusive:
        print("  INCONCLUSIVE (timed out twice):", n)
    return 1 if survived or errors or inconclusive else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
