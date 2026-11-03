"""Stopping and removing: the three meanings of "take it away".

  stop    every harness process this orchestrator started is gone; the data stays.
  purge   the same, and then the whole home folder is deleted.
  (removing the code is just deleting `src/superagent/`; it needs no command, and the harness
   does not notice.)

Neither needs the database to be healthy: processes are found from the manifest AND by a scan
for command lines that name the home folder, because the emergency path must not depend on the
normal one.
"""
import json
import os
import shutil
import sqlite3
import time

from . import executor as ex
from . import procutil
from .store import lock_is_held


def manifest_rows(home):
    """Processes recorded in the database, read without taking the lock. [] if unreadable."""
    path = os.path.join(home, "state.sqlite")
    if not os.path.exists(path):
        return []
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            cur = db.execute("SELECT pid, create_time, ticket_id FROM process")
            return [dict(pid=r[0], create_time=r[1], ticket_id=r[2]) for r in cur.fetchall()]
        finally:
            db.close()
    except sqlite3.Error:
        return []


def stop_processes(home):
    """Kill every process that is provably ours. -> list of pids killed."""
    home = os.path.abspath(home)
    killed = []
    for row in manifest_rows(home):
        if procutil.is_ours(row["pid"], row["create_time"], home):
            ex.kill_pid(row["pid"])
            killed.append(row["pid"])
    # the scan is deliberately narrow: our own harness children, and nothing that merely mentions
    # the folder (an editor with a file open, a backup tool, a shell)
    for pid in procutil.find_by_cmdline(home, "--jsonl", "harness_vllm"):
        if pid not in killed and pid != os.getpid():
            ex.kill_pid(pid)
            killed.append(pid)
    deadline = time.time() + 15
    while killed and any(procutil.create_time(p) is not None for p in killed) and time.time() < deadline:
        time.sleep(0.1)
    return killed


def running(home):
    """True when an orchestrator holds the home lock."""
    return lock_is_held(home)


def down(home, mode="stop", wait_for_engine=30.0):
    """mode 'stop' or 'purge'. -> a dict describing what was done. Raises RuntimeError when a
    live orchestrator does not go away in time."""
    home = os.path.abspath(home)
    if mode not in ("stop", "purge"):
        raise ValueError("mode must be 'stop' or 'purge'")
    if not os.path.isdir(home):
        return {"killed": [], "removed": False}
    if running(home):
        from .control import Channel
        ch = Channel(home)
        name = ch.submit("shutdown")
        res = ch.wait(name, wait_for_engine)
        if res is None:
            raise RuntimeError("the orchestrator did not answer the shutdown request")
        deadline = time.time() + wait_for_engine
        while running(home) and time.time() < deadline:
            time.sleep(0.2)
        if running(home):
            raise RuntimeError("the orchestrator is still running")
    killed = stop_processes(home)
    removed = False
    if mode == "purge":
        for attempt in range(20):
            try:
                shutil.rmtree(home)
                removed = True
                break
            except OSError:
                time.sleep(0.25)
        if not removed and os.path.exists(home):
            raise RuntimeError(f"could not remove {home}")
    return {"killed": killed, "removed": removed}
