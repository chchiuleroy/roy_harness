"""One command that runs every test suite this repository has (standard library only).

    python verify.py                 harness suite, then the kb suite; both always run
    python verify.py --only kb       just one stage (harness | kb)

Why it exists: the repository has two test suites with different commands. The harness and the superagent layer share
`tests/` and need no packages; kb is a separate optional layer with its own `kb/run_tests.py` and two optional packages
(numpy, PyMuPDF). CI runs them as two jobs. This script is the same thing for a local run, so nobody (a person or an
agent) can report "verified" after running only half.

Rules it keeps, and why:
  * Both stages always run. A harness failure must not hide a kb failure (or the other way round).
  * A stage that cannot run is a failure, not a skip. kb without numpy/PyMuPDF would otherwise show 52 confusing errors
    (README, section on tests), and a silent "skipped" would let "verify passed" mean "half was never run".
  * Live switches (HARNESS_LIVE, SUPERAGENT_LIVE, SUPERAGENT_REMOVAL) are removed from the child environment. This is the
    offline check; a stray variable in your shell must not send test traffic to a real server or make the run slow.
  * Exit code 0 only when every selected stage passed. The summary is for people; the exit code is what to rely on.

It does not install anything and reads no .harness.env.
"""
import argparse
import importlib.util
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
LIVE_SWITCHES = ("HARNESS_LIVE", "SUPERAGENT_LIVE", "SUPERAGENT_REMOVAL")
KB_OPTIONAL = ("numpy", "fitz")  # PyMuPDF is imported as fitz
RAN = re.compile(r"Ran (\d+) tests? in ([\d.]+)s")


def default_stages(root=ROOT):
    return [
        {"name": "harness", "cwd": root, "needs": (),
         "cmd": [sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests"]},
        {"name": "kb", "cwd": os.path.join(root, "kb"), "needs": KB_OPTIONAL,
         "cmd": [sys.executable, "-X", "utf8", "run_tests.py"]},
    ]


def run_stage(stage, env, find=importlib.util.find_spec, out=sys.stdout):
    """Run one stage; returns {name, ok, reason, tests}. Never raises for a failing or unrunnable stage."""
    missing = [m for m in stage["needs"] if find(m) is None]
    if missing:
        return {"name": stage["name"], "ok": False, "tests": None,
                "reason": f"not run: missing optional package(s) {', '.join(missing)} for this Python "
                          f"({sys.executable}); a stage that cannot run counts as a failure"}
    out.write(f"\n=== {stage['name']}: {' '.join(stage['cmd'])}  (in {stage['cwd']}) ===\n")
    out.flush()
    tests = None
    with subprocess.Popen(stage["cmd"], cwd=stage["cwd"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace") as proc:
        for line in proc.stdout:
            out.write(line)
            m = RAN.search(line)
            if m:
                tests = int(m.group(1))
        code = proc.wait()
    return {"name": stage["name"], "ok": code == 0, "tests": tests,
            "reason": "" if code == 0 else f"exit code {code}"}


def main(argv=None, stages=None, find=importlib.util.find_spec, out=sys.stdout, environ=None):
    ap = argparse.ArgumentParser(description="Run every test suite of this repository (offline).")
    ap.add_argument("--only", choices=("harness", "kb"), help="run just this stage")
    args = ap.parse_args(argv)
    stages = default_stages() if stages is None else stages
    if args.only:
        stages = [s for s in stages if s["name"] == args.only]
    env = dict(os.environ if environ is None else environ)
    dropped = [k for k in LIVE_SWITCHES if env.pop(k, None) is not None]
    if dropped:
        out.write(f"note: removed {', '.join(dropped)} from the test environment (verify.py is the offline check)\n")
    started = time.time()
    results = [run_stage(s, env, find, out) for s in stages]
    out.write("\n=== summary ===\n")
    for r in results:
        count = "" if r["tests"] is None else f"  ({r['tests']} tests)"
        out.write(f"{'OK  ' if r['ok'] else 'FAIL'} {r['name']}{count}{('  ' + r['reason']) if r['reason'] else ''}\n")
    ok = bool(results) and all(r["ok"] for r in results)
    out.write(f"{'verify: PASS' if ok else 'verify: FAIL'}  ({time.time() - started:.0f}s)\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
