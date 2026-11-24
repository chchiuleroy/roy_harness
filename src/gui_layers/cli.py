"""Run one command line of an optional layer and bring back its text. Shared by the panels."""
import os
import subprocess
import sys

MAX_OUTPUT = 20_000                      # what one panel shows of one command; the rest is cut and says so


def run_cli(args, pythonpath, cwd, timeout):
    """`python -X utf8 <args>` with `pythonpath` on the module path -> (returncode, stdout, stderr).
    returncode is None when the command did not finish in `timeout` seconds (it is killed), and
    OSError is turned into returncode -1 with the reason as stderr: a panel shows it, never crashes."""
    env = dict(os.environ, PYTHONPATH=pythonpath, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run([sys.executable, "-X", "utf8", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return None, "", f"no answer in {timeout} seconds"
    except OSError as e:
        return -1, "", str(e)
    return proc.returncode, clip(proc.stdout.decode("utf-8", "replace")), clip(proc.stderr.decode("utf-8", "replace"))


def clip(text):
    text = text.replace("\r\n", "\n")        # Windows consoles end lines with CRLF; the page shows plain text
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n…（共 {len(text)} 字元，只顯示前段）"
