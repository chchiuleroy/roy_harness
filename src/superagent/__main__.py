"""Command line:  python -X utf8 -m superagent --home DIR <command>   (run from src/, or set PYTHONPATH=src)

  run        start the orchestrator (stays in the foreground; Ctrl-C stops it cleanly)
  status     show tickets, open questions and the outbox (works while it runs)
  decisions  show recorded decisions (read-only);  --for-labeling exports the messages without the answers
  label      make a single offline HTML page for labelling messages by hand (no model answers in it)
  eval       replay a labelled corpus through the model session offline and report (a measurement, not a verdict)
  report     print a saved eval report as readable text;  --errors --corpus FILE lists the rows the model got wrong
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


def parse_rules(texts):
    rules = []
    for text in texts:
        name, sep, instruction = text.partition("=")
        if not sep:
            raise SystemExit(f"--rule needs NAME=description, got {text!r}")
        try:
            rules.append(Rule(name.strip(), instruction.strip()))
        except ValueError as e:
            raise SystemExit(f"--rule: {e}")
    return rules


def build_engine(args, store):
    cfg = ExecutorConfig(
        provider=args.provider, base_url=args.base_url, model=args.model,
        api_key=os.environ.get("SUPERAGENT_API_KEY", ""), backoff=args.backoff)
    rules = parse_rules(args.rule)
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
                      eval_timeout=args.eval_timeout, idle_end=args.idle_end)
    except ValueError as e:
        raise SystemExit(f"invalid setting: {e}")


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


def work_left(engine, store, model=True):
    """What `run --once` waits for: a ticket still running, a reply not yet delivered, an event file still in
    the inbox, or a message still waiting for (or being judged by) the model session. The last two were
    missing: with --assessor model the loop used to exit after the first tick with every candidate pending.
    model=False leaves out the candidates only the model session can judge (it cannot start: see cmd_run)."""
    counts = store.eval_counts()
    folder = getattr(engine.source, "folder", None)               # only the file inbox has one
    inbox_left = folder is not None and any(n.endswith(".json") for n in os.listdir(folder))
    return bool(engine.runs) or bool(store.outbox_rows("pending")) or inbox_left \
        or (model and counts["pending"] + counts["sent"] > 0)


def cmd_run(args):
    store = Store(args.home).open()
    engine = build_engine(args, store)
    ch = Channel(args.home)
    stop = threading.Event()
    blocked = None
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    try:
        engine.recover()
        print(f"[superagent] running on {store.home}; Ctrl-C to stop", flush=True)
        while not stop.is_set():
            for name, cmd in ch.take():
                if dispatch(engine, ch, name, cmd):
                    stop.set()
            engine.tick()
            if args.once and not work_left(engine, store):
                break
            if args.once and engine.main_blocked and not work_left(engine, store, model=False):
                blocked = engine.main_blocked          # nothing else is going on and nothing will judge the rest
                left = store.eval_counts()["pending"]
                break
            time.sleep(args.interval)
    finally:
        engine.shutdown()
        store.close()
    if blocked:
        print(f"[superagent] run --once stopped: the model session cannot start ({blocked}); "
              f"{left} message(s) were left pending for a later run", file=sys.stderr)
        return 1
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


def cmd_decisions(args):
    from . import decisions_view as dv
    try:
        since = dv.parse_since(args.since) if args.since else None
        rows = dv.read_decisions(args.home, since=since, verdict=args.verdict,
                                 would_accept=True if args.would_accept else None, limit=args.limit,
                                 candidates_only=args.for_labeling)
        if args.for_labeling or args.jsonl:
            out = dv.to_jsonl(rows, text_cap=args.text_cap, blind=args.for_labeling, card_cap=args.card_cap)
        else:
            out = dv.to_table(rows)
    except dv.DecisionsError as e:
        print(f"[superagent] {e}", file=sys.stderr)
        return 1
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    return 0


def cmd_eval(args):
    from . import corpus as corpus_mod
    from . import replay as rp
    from .sanitize import json_safe
    rules = parse_rules(args.rule)
    if args.out:                  # a bad path found after the model work would waste it (the JSON is still printed then)
        parent = os.path.dirname(os.path.abspath(args.out))
        if not os.path.isdir(parent) or os.path.isdir(args.out):
            print(f"[superagent] eval refused:\n  --out {args.out}: the folder {parent} does not exist "
                  f"(or the path is a folder); nothing was run", file=sys.stderr)
            return 1
    cfg = rp.ReplayConfig(
        provider=args.provider, base_url=args.base_url, model=args.model, rules=tuple(rules),
        repeats=args.repeats, order_check=args.order_check, batch_max=args.batch_max, card_cap=args.card_cap,
        half_width=args.half_width, confidence=args.confidence, eval_timeout=args.eval_timeout,
        chunk_timeout=args.chunk_timeout, backoff=args.backoff, hash_ids=args.hash_ids,
        progress=None if args.quiet else (lambda text: print(f"[eval] {text}", file=sys.stderr, flush=True)))
    try:
        cfg.validate()
        corpus = corpus_mod.load(args.corpus, cfg.rules, cfg.card_cap, allow_shared_threads=args.allow_shared_threads,
                                 allow_truncated=args.allow_truncated)
    except (ValueError, corpus_mod.CorpusError) as e:
        problems = getattr(e, "problems", None) or [str(e)]
        print("[superagent] eval refused:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    report = rp.replay(corpus, cfg)
    text = json_safe(json.dumps(report, indent=1, ensure_ascii=False))
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8", newline="\n") as f:
                f.write(text + "\n")
        except OSError as e:
            print(f"[superagent] cannot write the report to {args.out}: {e}\n"
                  f"  the finished report is printed below on stdout; save it from there",
                  file=sys.stderr)
            print(text)                       # an hour of model calls must not be lost to a bad path
            return 1
        print(f"[superagent] report written to {args.out}")
        from . import summary
        texts = summary.snippets(args.corpus, hash_ids=args.hash_ids) if args.errors else None
        sys.stdout.write("\n" + summary.render(report, errors=args.errors, texts=texts))
    else:
        print(text)
    return 0


def cmd_report(args):
    from . import summary
    try:
        with open(args.report, encoding="utf-8") as f:
            report = json.load(f)
        texts = summary.snippets(args.corpus, hash_ids=bool(report["meta"].get("hash_ids"))) if args.corpus else None
        sys.stdout.write(summary.render(report, errors=args.errors, texts=texts))
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"[superagent] cannot read the report: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_label(args):
    from . import labeler
    rules = parse_rules(args.rule)
    if not rules:
        raise SystemExit("label needs at least one --rule NAME=DESCRIPTION")
    try:
        if isinstance(args.card_cap, bool) or args.card_cap < 1:
            raise labeler.LabelerError("card_cap must be a whole number, 1 or more")
        rows = labeler.read_unlabelled(args.input, args.card_cap)
        page = labeler.build_html(rows, rules, title=args.title)
    except (OSError, labeler.LabelerError) as e:
        print(f"[superagent] label refused: {e}", file=sys.stderr)
        return 1
    try:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(page)
    except OSError as e:
        print(f"[superagent] cannot write the page to {args.out}: {e}", file=sys.stderr)
        return 1
    cut = sum(1 for r in rows if r["truncated_chars"])
    print(f"[superagent] {len(rows)} messages ({cut} cut at {args.card_cap} characters) -> {args.out}\n"
          f"  open it in a browser, label, then use the 'download labelled' button and run eval on that file")
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
    r.add_argument("--once", action="store_true", help="exit when nothing is running, the outbox is empty, the inbox has no event files left and "
                        "no message is waiting for the model (exit 1 if the model session cannot start)")
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
    r.add_argument("--idle-end", type=float, default=600.0,
                   help="seconds of inactivity after which a ticket with nothing left to do ends and frees its slot")
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
    v = sub.add_parser("decisions", help="show the recorded decisions (read-only; works while it runs)")
    v.add_argument("--since", help="epoch seconds or an ISO time")
    v.add_argument("--verdict", help="accept, ignore or unavailable")
    v.add_argument("--would-accept", action="store_true", help="only observe-mode rows where a rule matched")
    v.add_argument("--limit", type=int, help="the latest N rows")
    v.add_argument("--jsonl", action="store_true", help="one JSON object per line instead of a table")
    v.add_argument("--text-cap", type=int, default=200)
    v.add_argument("--for-labeling", action="store_true",
                   help="blind JSONL of the messages the model saw, without what it answered (implies --jsonl)")
    v.add_argument("--card-cap", type=int, default=CARD_CAP, help="characters of text the model sees per card (for --for-labeling); the home does not "
                        "record the cap a past run used, so give the one it ran with")
    e = sub.add_parser("eval", help="replay a labelled corpus through the model session, offline, and report")
    e.add_argument("--corpus", required=True, help="JSONL file: id, text, label (rule name or null), optional uncertain/sender/thread/note")
    e.add_argument("--rule", action="append", default=[], metavar="NAME=DESCRIPTION", required=True)
    e.add_argument("--provider", default=os.environ.get("HARNESS_PROVIDER", "vllm"))
    e.add_argument("--base-url", default=os.environ.get("HARNESS_BASE_URL", ""))
    e.add_argument("--model", default=os.environ.get("HARNESS_MODEL", ""))
    e.add_argument("--repeats", type=int, default=1)
    e.add_argument("--order-check", action="store_true", help="also replay each batch in reverse order")
    e.add_argument("--hash-ids", action="store_true", help="replace row ids by a short hash in the report")
    e.add_argument("--batch-max", type=int, default=BATCH_MAX)
    e.add_argument("--card-cap", type=int, default=CARD_CAP)
    e.add_argument("--half-width", type=float, default=0.10, help="target half-width for the rows-needed estimate")
    e.add_argument("--confidence", type=float, default=0.95)
    e.add_argument("--eval-timeout", type=float, default=180.0)
    e.add_argument("--chunk-timeout", type=float, default=600.0)
    e.add_argument("--backoff", type=float, default=1.0)
    e.add_argument("--allow-truncated", action="store_true", help="accept rows longer than the card cap")
    e.add_argument("--allow-shared-threads", action="store_true", help="accept several rows in one thread")
    e.add_argument("--out", help="write the JSON report here (a readable summary is then printed) instead of stdout")
    e.add_argument("--errors", action="store_true", help="with --out: also list the rows the model got wrong")
    e.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    lb = sub.add_parser("label", help="make an offline HTML page for labelling messages")
    lb.add_argument("--input", required=True, help="JSONL (text per line, optional id) or a text file, one message per line")
    lb.add_argument("--rule", action="append", default=[], metavar="NAME=DESCRIPTION", required=True)
    lb.add_argument("--out", required=True, help="the HTML file to write")
    lb.add_argument("--card-cap", type=int, default=CARD_CAP, help="characters of text the model sees per card")
    lb.add_argument("--title", default="標註訊息")
    rp_ = sub.add_parser("report", help="print a saved eval report as readable text")
    rp_.add_argument("report", help="the JSON file eval wrote with --out")
    rp_.add_argument("--errors", action="store_true", help="list the rows the model got wrong")
    rp_.add_argument("--corpus", help="the corpus file, to show the first words of those messages (terminal only)")
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
    if args.cmd == "decisions":
        return cmd_decisions(args)
    if args.cmd == "label":
        return cmd_label(args)
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "eval":
        if not args.base_url or not args.model:
            ap.error("eval needs --base-url and --model (or HARNESS_BASE_URL / HARNESS_MODEL)")
        return cmd_eval(args)
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
