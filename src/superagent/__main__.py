"""Command line:  python -X utf8 -m superagent --home DIR <command>   (run from src/, or set PYTHONPATH=src)

  run        start the orchestrator (stays in the foreground; Ctrl-C stops it cleanly)
  status     show tickets, open questions and the outbox (works while it runs)
  answer     answer a question:   answer <ask_id> y|n
  stop       stop a ticket for good
  resume     clear the note on a ticket that was parked, so it starts again
  down       stop every process;  --mode purge also deletes the home folder
"""
import argparse
import json
import os
import signal
import sys
import threading
import time

from . import lifecycle
from .control import Channel
from .engine import Engine
from .executor import ExecutorConfig, HarnessExecutor
from .gate import Policy, Rule, RuleAssessor
from .main_session import BATCH_MAX, CARD_CAP, MainSession
from .sources import FileInbox, FileSink
from .store import AskRejected, Store, StoreLocked

DEFAULT_HOME = "C:/Users/roy/superagent_home"


def build_engine(args, store):
    cfg = ExecutorConfig(
        provider=args.provider, base_url=args.base_url, model=args.model,
        api_key=os.environ.get("SUPERAGENT_API_KEY", ""), backoff=args.backoff)
    rules = []
    for text in args.rule:
        name, sep, instruction = text.partition("=")
        if not sep:
            raise SystemExit(f"--rule needs NAME=description, got {text!r}")
        try:
            rules.append(Rule(name.strip(), instruction.strip()))
        except ValueError as e:
            raise SystemExit(f"--rule: {e}")
    policy = Policy(allowed_senders=frozenset(filter(None, args.allow.split(","))),
                    bot_senders=frozenset(filter(None, args.bots.split(","))),
                    accept_mode=args.accept_mode, rules=tuple(rules))
    home = store.home
    inbox = FileInbox(args.inbox or os.path.join(home, "inbox"))
    sink = FileSink(args.outbox or os.path.join(home, "outbox"))
    main = None
    if args.assessor == "model":
        main_cfg = ExecutorConfig(
            provider=args.provider, base_url=args.main_base_url or args.base_url,
            model=args.main_model or args.model, api_key=os.environ.get("SUPERAGENT_API_KEY", ""),
            backoff=args.backoff)
        main = MainSession(main_cfg, policy, home, lambda label: HarnessExecutor(main_cfg),
                           reset_every=args.main_reset_batches)
    if args.main_reset_batches < 1:
        raise SystemExit("--main-reset-batches must be 1 or more")
    try:
        return Engine(store, inbox, sink, policy, RuleAssessor(), lambda tid: HarnessExecutor(cfg),
                      ask_timeout=args.ask_timeout, max_running=args.max_running, main=main,
                      batch_max=args.batch_max, batch_min=args.batch_min, batch_wait=args.batch_wait,
                      eval_timeout=args.eval_timeout)
    except ValueError as e:
        raise SystemExit(f"--assessor model: {e}")


def dispatch(engine, ch, name, cmd):
    try:
        kind = cmd.get("cmd")
        if kind == "answer":
            engine.answer_ask(int(cmd["ask_id"]), bool(cmd["yes"]))
            ch.reply(name, True, "answered")
        elif kind == "stop":
            engine.stop_ticket(int(cmd["ticket"]))
            ch.reply(name, True, "stopped")
        elif kind == "resume":
            engine.resume(int(cmd["ticket"]))
            ch.reply(name, True, "resumed")
        elif kind == "shutdown":
            engine.shutdown()
            ch.reply(name, True, "shutting down")
            return True
        else:
            ch.reply(name, False, f"unknown command {kind!r}")
    except (AskRejected, KeyError, ValueError, TypeError) as e:
        ch.reply(name, False, f"{type(e).__name__}: {e}")
    return False


def cmd_run(args):
    store = Store(args.home).open()
    engine = build_engine(args, store)
    ch = Channel(args.home)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    try:
        engine.recover()
        print(f"[superagent] running on {store.home}; Ctrl-C to stop", flush=True)
        while not stop.is_set():
            for name, cmd in ch.take():
                if dispatch(engine, ch, name, cmd):
                    stop.set()
            engine.tick()
            if args.once and not engine.runs and not store.outbox_rows("pending"):
                break
            time.sleep(args.interval)
    finally:
        engine.shutdown()
        store.close()
    return 0


def cmd_status(args):
    import sqlite3
    path = os.path.join(args.home, "state.sqlite")
    if not os.path.exists(path):
        print("no state yet")
        return 1
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    out = {
        "tickets": [dict(r) for r in db.execute("SELECT id, state, thread, note, run_gen FROM ticket")],
        "open_asks": [dict(r) for r in db.execute(
            "SELECT ask_id, ticket_id, what FROM ask WHERE state='open'")],
        "outbox": {r[0]: r[1] for r in db.execute("SELECT status, COUNT(*) FROM outbox GROUP BY status")},
        "running": lifecycle.running(args.home),
    }
    try:                                   # homes made before the model session have no such table
        out["eval"] = {r[0]: r[1] for r in db.execute("SELECT state, COUNT(*) FROM eval_item GROUP BY state")}
    except sqlite3.OperationalError:
        pass
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


def send(args, cmd, **kw):
    ch = Channel(args.home)
    res = ch.wait(ch.submit(cmd, **kw), args.wait)
    if res is None:
        print("no answer: is the orchestrator running?", file=sys.stderr)
        return 2
    print(res["message"])
    return 0 if res["ok"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="superagent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=os.environ.get("SUPERAGENT_HOME", DEFAULT_HOME))
    ap.add_argument("--wait", type=float, default=30.0, help="seconds to wait for a running orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--once", action="store_true", help="exit when nothing is running and the outbox is empty")
    r.add_argument("--interval", type=float, default=0.5)
    r.add_argument("--inbox")
    r.add_argument("--outbox")
    r.add_argument("--allow", default="", help="comma-separated senders who may ask for work")
    r.add_argument("--bots", default="")
    r.add_argument("--accept-mode", choices=("off", "observe", "auto"), default="observe")
    r.add_argument("--provider", default=os.environ.get("HARNESS_PROVIDER", "vllm"))
    r.add_argument("--base-url", default=os.environ.get("HARNESS_BASE_URL", ""))
    r.add_argument("--model", default=os.environ.get("HARNESS_MODEL", ""))
    r.add_argument("--backoff", type=float, default=1.0)
    r.add_argument("--ask-timeout", type=float, default=3600.0)
    r.add_argument("--max-running", type=int, default=4)
    r.add_argument("--assessor", choices=("rules", "model"), default="rules",
                   help="who judges messages that are not direct requests: keyword rules (default) or a model session")
    r.add_argument("--rule", action="append", default=[], metavar="NAME=DESCRIPTION",
                   help="a proactive rule for the model to match (repeatable; name: a-z, 0-9, -)")
    r.add_argument("--batch-max", type=int, default=BATCH_MAX)
    r.add_argument("--batch-min", type=int, default=1)
    r.add_argument("--batch-wait", type=float, default=3.0)
    r.add_argument("--eval-timeout", type=float, default=180.0)
    r.add_argument("--main-reset-batches", type=int, default=1,
                   help="start a fresh model session every N batches (1 = every batch; larger keeps context between batches)")
    r.add_argument("--main-model")
    r.add_argument("--main-base-url")
    sub.add_parser("status")
    a = sub.add_parser("answer")
    a.add_argument("ask_id", type=int)
    a.add_argument("choice", choices=("y", "n"))
    s = sub.add_parser("stop")
    s.add_argument("ticket", type=int)
    s = sub.add_parser("resume")
    s.add_argument("ticket", type=int)
    d = sub.add_parser("down")
    d.add_argument("--mode", choices=("stop", "purge"), default="stop")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        if not args.base_url or not args.model:
            ap.error("run needs --base-url and --model (or HARNESS_BASE_URL / HARNESS_MODEL)")
        try:
            return cmd_run(args)
        except StoreLocked as e:
            print(f"[superagent] {e}", file=sys.stderr)
            return 3
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "answer":
        return send(args, "answer", ask_id=args.ask_id, yes=args.choice == "y")
    if args.cmd == "stop":
        return send(args, "stop", ticket=args.ticket)
    if args.cmd == "resume":
        return send(args, "resume", ticket=args.ticket)
    if args.cmd == "down":
        print(json.dumps(lifecycle.down(args.home, args.mode)))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
