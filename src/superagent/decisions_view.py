"""Looking at the decisions an orchestrator has recorded. Read-only.

The database is opened with `mode=ro`, so nothing here can change it. That is only a promise worth
making for a home made by this package (an ordinary rollback-journal database): the journal mode is
checked first and a write-ahead-log home is refused rather than read. A home whose last writer died
in the middle of a write has a hot journal that a read-only connection cannot roll back; that is
reported, never repaired.

Everything that comes from outside (sender, thread, text) or from the model (rule, reason) is untrusted
text: tables spell the unsafe characters out, and the JSON lines spell them as \\uXXXX, so a message
cannot rearrange what the reader sees.
"""
import datetime
import json
import os
import sqlite3

from . import sanitize
from .model import Verdict

SCHEMA = 1


class DecisionsError(Exception):
    pass


def parse_since(text):
    """An epoch number, or an ISO 8601 date/time (a time without a zone is local time) -> epoch."""
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.datetime.fromisoformat(str(text))
    except ValueError:
        raise DecisionsError(f"--since {text!r} is neither an epoch number nor an ISO 8601 time") from None
    return dt.timestamp()


def _is_wal(path):
    """True when the database file says it is in write-ahead-log mode. Read from the file header
    itself (bytes 18 and 19 are 2 for WAL, 1 for a rollback journal): asking SQLite with a PRAGMA would
    already create the -shm file this check exists to avoid."""
    with open(path, "rb") as f:
        head = f.read(20)
    return len(head) == 20 and head.startswith(b"SQLite format 3\x00") and (head[18] == 2 or head[19] == 2)


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def read_decisions(home, since=None, verdict=None, would_accept=None, limit=None, candidates_only=False):
    """-> list of dicts, oldest first. `limit` keeps the LATEST n. `candidates_only`: only decisions of
    messages that went to the model (the ones a labeller can judge)."""
    path = os.path.join(home, "state.sqlite")
    if not os.path.exists(path):
        raise DecisionsError("no state yet: this home has never been run")
    if verdict is not None and verdict not in {v.value for v in Verdict}:
        raise DecisionsError(f"unknown verdict {verdict!r}; choose from {sorted(v.value for v in Verdict)}")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise DecisionsError("limit must be a whole number, 1 or more")
    if _is_wal(path):
        raise DecisionsError("this database is in write-ahead-log mode; only homes made by this package (a "
                             "rollback journal) are read, because reading a WAL database can create files next to it")
    db = None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        tables = _tables(db)
        if "decision" not in tables:
            raise DecisionsError("this home has no decision table")
        cols = {r[1] for r in db.execute("PRAGMA table_info(decision)")}
        model_rule = "d.model_rule" if "model_rule" in cols else "NULL"
        where, args = [], []
        if since is not None:
            where.append("d.ts >= ?")
            args.append(since)
        if verdict is not None:
            where.append("d.verdict = ?")
            args.append(verdict)
        if would_accept:
            where.append("d.would_accept = 1")
        if candidates_only:
            where.append("d.trigger_id IN (SELECT trigger_id FROM eval_item)" if "eval_item" in tables else "0")
        sql = ("SELECT d.id AS decision_id, d.trigger_id, d.ts, t.source, t.thread, t.sender, t.text, d.verdict, "
               f"d.strength, d.would_accept, {model_rule} AS model_rule, d.reason "
               "FROM decision d LEFT JOIN trigger t ON t.id = d.trigger_id"
               + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY d.id DESC" + (f" LIMIT {int(limit)}" if limit else ""))
        rows = [dict(r) for r in db.execute(sql, args)]
    except sqlite3.OperationalError as e:
        raise DecisionsError(f"the home cannot be read ({e}); if the orchestrator stopped in the middle of a "
                             f"write, start it once or remove the journal file yourself") from e
    finally:
        if db is not None:
            db.close()
    rows.reverse()
    for r in rows:
        r["would_accept"] = bool(r["would_accept"])
    return rows


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_jsonl(rows, text_cap=200, blind=False, card_cap=1000):
    """One JSON object per line.

    blind=True is for a person who is going to label the messages: no verdict, no rule, no reason (what
    the model said must not steer them), and the text is what the model is shown under `card_cap`, with how
    much was cut off. The home does not record the cap a past run used, so `card_cap` is the caller's
    claim (the line says which one was used). Two choices keep the file honest:
    * `id` is an opaque c0001, c0002 ... in file order, not the database key, so the labelling file cannot
      be joined back to the model's answers by that key (it can still be matched on the text itself: this
      guards against being steered, not against someone who goes looking);
    * there is no `label` key at all, so a row that was never labelled is refused by `corpus.load`
      instead of silently counting as 'no rule applies'."""
    for name, v in (("text_cap", text_cap), ("card_cap", card_cap)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise DecisionsError(f"{name} must be a whole number, 1 or more")
    out = []
    for i, r in enumerate(rows, 1):
        text = r["text"] if isinstance(r["text"], str) else ""
        if blind:
            obj = {"schema": SCHEMA, "id": f"c{i:04d}", "source": r["source"], "thread": r["thread"],
                   "sender": r["sender"], "text": text[:card_cap], "truncated_chars": max(0, len(text) - card_cap),
                   "card_cap": card_cap, "uncertain": False}
        else:
            obj = {"schema": SCHEMA, "decision_id": r["decision_id"], "trigger_id": r["trigger_id"],
                   "ts": r["ts"], "source": r["source"], "thread": r["thread"], "sender": r["sender"],
                   "text": text[:text_cap], "text_truncated": len(text) > text_cap, "verdict": r["verdict"],
                   "strength": r["strength"], "would_accept": r["would_accept"], "model_rule": r["model_rule"],
                   "reason": r["reason"]}
        out.append(sanitize.json_safe(json.dumps(obj, ensure_ascii=False)))
    return "\n".join(out) + ("\n" if out else "")


def _cell(value, width):
    text = sanitize.visible("" if value is None else value)
    return (text if len(text) <= width else text[:width - 1] + "~").ljust(width)


def to_table(rows, text_cap=60):
    head = ["id", "time (UTC)", "verdict", "wa", "model_rule", "sender", "text"]
    widths = [5, 20, 11, 2, 14, 12, text_cap]
    lines = ["  ".join(h.ljust(w) for h, w in zip(head, widths))]
    for r in rows:
        mr = "-" if r["model_rule"] is None else ("(none)" if r["model_rule"] == "" else r["model_rule"])
        cells = [r["decision_id"], _iso(r["ts"]), r["verdict"], "y" if r["would_accept"] else "", mr, r["sender"],
                 r["text"]]
        lines.append("  ".join(_cell(c, w) for c, w in zip(cells, widths)).rstrip())
    return "\n".join(lines) + "\n"
