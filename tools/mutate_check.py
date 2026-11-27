"""Mutation check for the harness code (standard library only).

Break the code on purpose, in a throwaway copy, and require the named test to go red. A mutant that survives
means that test does not guard the behaviour it claims to guard.

    python tools/mutate_check.py --list            show the mutants
    python tools/mutate_check.py                   run all of them (4 at a time)
    python tools/mutate_check.py NAME [NAME ...]   run only these

Each mutant is (name, file under the repository root, old text, new text, test id). The old text must occur exactly
once in the file. "killed" means the test failed an assertion; a mutant that makes it crash for another reason is
"errored" and counts as not killed. The test id is what `python -m unittest` takes, run from the copy's `tests/` folder. Before any
mutant runs, the same test is run on the unmutated copy and must pass: otherwise "red" would say nothing.

kb has its own tool (kb/tools/mutate.py) that knows kb's layout; this one is for src/harness and src/superagent.
It is slow (a few seconds per mutant) and is not part of verify.py. Exit code 0 only if every mutant was killed.
"""
import concurrent.futures as cf
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COPY_DIRS = ("src", "tests")
TIMEOUT_S = 300

PROV = "test_harness_provenance.TestHostsIn.test_new_patterns_miss_no_host_the_old_ones_found"
ORIGIN = "src/harness/harness_origin.py"
MUTANTS = [
    ("origin_url_scheme_must_end_in_a_letter", ORIGIN, "(?<=[a-z0-9+.-])://", "(?<=[a-z])://", PROV),
    ("origin_url_host_loses_digits", ORIGIN, r"(\[[0-9a-f:.]+\]|[\w.-]+)", r"(\[[0-9a-f:.]+\]|[a-z.-]+)", PROV),
    ("origin_at_host_needs_a_word_char_before_the_at", ORIGIN, r"(?<=[\w.+-])@", r"(?<=[\w])@", PROV),
    ("origin_at_host_needs_two_dots", ORIGIN, r'(?:\.[a-z0-9-]+)+)", re.I)    # mail', r'(?:\.[a-z0-9-]+){2,})", re.I)    # mail', PROV),
    ("origin_at_host_label_loses_digits", ORIGIN, r"@([a-z0-9-]+(?:", r"@([a-z0-]+(?:", PROV),
    ("origin_keeps_the_trailing_dot", ORIGIN, '.lower().strip(".")', ".lower()", PROV),
    ("origin_drops_every_host", ORIGIN, "if h and any(c.isalnum() for c in h)", "if h and False", PROV),
]


def prepare(root, work, copy_dirs=COPY_DIRS):
    for d in copy_dirs:
        shutil.copytree(os.path.join(root, d), os.path.join(work, d), ignore=shutil.ignore_patterns("__pycache__"))


def run_test(work, test_id, timeout=TIMEOUT_S):
    env = {k: v for k, v in os.environ.items() if not k.endswith("_LIVE") and k != "SUPERAGENT_REMOVAL"}
    p = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", test_id], cwd=os.path.join(work, "tests"),
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)[-3000:]


def check_mutant(mutant, root=ROOT, copy_dirs=COPY_DIRS, timeout=TIMEOUT_S):
    """-> (name, verdict, detail). verdict: killed | SURVIVED | invalid | baseline-failed | errored"""
    name, rel, old, new, test_id = mutant
    work = tempfile.mkdtemp(prefix="harness_mut_")
    try:
        prepare(root, work, copy_dirs)
        path = os.path.join(work, rel)
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        if text.count(old) != 1:
            return name, "invalid", f"old text occurs {text.count(old)} times in {rel}"
        if old == new:
            return name, "invalid", "old and new text are the same"
        try:
            code, tail = run_test(work, test_id, timeout)
        except subprocess.TimeoutExpired:
            return name, "baseline-failed", f"the unmutated test timed out after {timeout} s"
        if code != 0:
            return name, "baseline-failed", tail
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old, new))
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            return name, "invalid", f"the mutant does not compile: {e}"
        code, tail = run_test(work, test_id, timeout)
        if code == 0:
            return name, "SURVIVED", tail
        if "AssertionError" not in tail:      # red for another reason (import error, crash): not evidence the test guards the behaviour
            return name, "errored", tail
        return name, "killed", tail
    except subprocess.TimeoutExpired:
        return name, "killed", f"timed out after {timeout} s (counts as red: the change made the test hang)"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--list" in argv:
        for m in MUTANTS:
            print(f"{m[0]}  ->  {m[4]}")
        return 0
    chosen = [m for m in MUTANTS if not argv or m[0] in argv]
    unknown = [a for a in argv if a not in {m[0] for m in MUTANTS}]
    if unknown or not chosen:
        print("unknown mutant name(s):", ", ".join(unknown) or "(none selected)")
        return 2
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(check_mutant, chosen))
    bad = 0
    for name, verdict, detail in results:
        print(f"{verdict:16} {name}")
        if verdict != "killed":
            bad += 1
            print("    " + detail.replace("\n", "\n    "))
    print(f"{len(results) - bad} of {len(results)} mutants killed")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
