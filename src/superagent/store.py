"""SQLite state, with exactly one writer.

`Store` is used from one thread only (the engine's); every other thread hands work over through
a queue. It takes an operating-system file lock on `<home>/.lock` for as long as it is open, so
a second orchestrator on the same home fails at once, and a crashed one never leaves a lock
behind (the OS drops it with the process).
"""
import contextlib
import dataclasses
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid

from .model import AskState, InputState, Mandate, Strength, TicketState

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class StoreLocked(Exception):
    pass


class AskRejected(Exception):
    """An answer that does not match a live question."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS trigger(
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, ext_id TEXT NOT NULL, thread TEXT NOT NULL,
  sender TEXT NOT NULL, text TEXT NOT NULL, addressed INTEGER NOT NULL, ts REAL NOT NULL,
  UNIQUE(source, ext_id));
CREATE TABLE IF NOT EXISTS ticket(
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, thread TEXT NOT NULL, state TEXT NOT NULL,
  run_gen INTEGER NOT NULL DEFAULT 0, cwd TEXT, session TEXT, created REAL NOT NULL,
  note TEXT);
CREATE TABLE IF NOT EXISTS mandate(
  ticket_id INTEGER PRIMARY KEY REFERENCES ticket(id), body TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS mandate_no_update BEFORE UPDATE ON mandate
  BEGIN SELECT RAISE(ABORT, 'a mandate is immutable'); END;
CREATE TRIGGER IF NOT EXISTS mandate_no_delete BEFORE DELETE ON mandate
  BEGIN SELECT RAISE(ABORT, 'a mandate is immutable'); END;
CREATE TABLE IF NOT EXISTS decision(
  id INTEGER PRIMARY KEY, trigger_id INTEGER NOT NULL REFERENCES trigger(id), verdict TEXT NOT NULL,
  strength TEXT NOT NULL, reason TEXT NOT NULL, would_accept INTEGER NOT NULL,
  ticket_id INTEGER, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS input(
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES ticket(id),
  trigger_id INTEGER NOT NULL REFERENCES trigger(id), delivery_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL, replayed INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ask(
  ask_id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, run_gen INTEGER NOT NULL,
  rid TEXT NOT NULL, what TEXT NOT NULL, what_sha TEXT NOT NULL, state TEXT NOT NULL, ts REAL NOT NULL,
  confirmed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL, msg_id TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS process(
  pid INTEGER NOT NULL, create_time REAL NOT NULL, ticket_id INTEGER NOT NULL, cwd TEXT NOT NULL,
  run_gen INTEGER NOT NULL, started REAL NOT NULL, PRIMARY KEY(pid, create_time));
"""


def lock_is_held(home, _hold=0.0):
    """True when an orchestrator holds the home lock. Looks at the lock only: it works even when
    the database file is damaged. Probes queue behind a guard lock (`.probe`), so two probes at
    the same moment never mistake each other for an orchestrator; `_hold` keeps a probe's lock a
    little longer, for tests."""
    probe = Store(home, lock_wait=0)
    os.makedirs(probe.home, exist_ok=True)
    try:
        guard = _acquire(os.path.join(probe.home, ".probe"), 2.0)
    except StoreLocked:
        guard = None                  # another probe is stuck; look anyway
    try:
        try:
            probe._take_lock()
        except StoreLocked:
            return True
        if _hold:
            time.sleep(_hold)
        probe._release_lock()
        return False
    finally:
        if guard is not None:
            _release(guard)


def _row(cur):
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _acquire(path, wait):
    """Take an exclusive OS lock on byte 0 of `path`, retrying for `wait` seconds.
    -> the open file (keep it open to keep the lock). Raises StoreLocked."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    f = os.fdopen(fd, "r+b", buffering=0)
    deadline = time.time() + wait
    while True:
        try:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError as e:
            if time.time() >= deadline:
                f.close()
                raise StoreLocked(f"{path} is locked") from e
            time.sleep(0.05)


def _release(f):
    try:
        f.seek(0)
        if os.name == "nt":
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    f.close()


class Store:
    def __init__(self, home, lock_wait=1.0):
        """lock_wait: how long open() keeps trying for the home lock. A short wait matters
        because `status` and `down` probe the lock for an instant, and a `run` that starts at
        that moment must not be told "in use" by a probe."""
        self.lock_wait = lock_wait
        self.home = os.path.abspath(home)
        self.db = None
        self._lock_file = None
        self._thread = None
        self._depth = 0
        self._failed = False

    # ---------------------------------------------------------------- lifecycle
    def open(self):
        os.makedirs(self.home, exist_ok=True)
        self._take_lock()
        try:
            self.db = sqlite3.connect(os.path.join(self.home, "state.sqlite"), isolation_level=None,
                                      check_same_thread=False)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.executescript(SCHEMA)
            self._thread = threading.get_ident()
            self._migrate()
        except BaseException:
            self.close()                    # do not keep the home lock or a half-open connection
            raise
        return self

    def _migrate(self):
        """`CREATE TABLE IF NOT EXISTS` never adds a column to a table that already exists, so a
        home made by an older version is brought up to date here, once, in one transaction."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(ask)")}
        if "confirmed" not in cols:
            with self.tx() as db:
                db.execute("ALTER TABLE ask ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")
                # whatever an older version recorded as answered is settled history, not an
                # answer still waiting to be seen to arrive
                db.execute("UPDATE ask SET confirmed=1")

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        self._thread = None
        self._release_lock()

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _take_lock(self):
        try:
            f = _acquire(os.path.join(self.home, ".lock"), self.lock_wait)
        except StoreLocked as e:
            raise StoreLocked(f"{self.home} is in use by another orchestrator") from e
        # owner note, for a human reading the file; it takes no part in locking
        f.seek(16)
        f.truncate(16)
        f.write(f"pid={os.getpid()} since={time.time():.0f}\n".encode("ascii"))
        self._lock_file = f

    def _release_lock(self):
        f, self._lock_file = self._lock_file, None
        if f is not None:
            _release(f)

    # ---------------------------------------------------------------- plumbing
    def _check(self):
        if self.db is None:
            raise RuntimeError("store is closed")
        if threading.get_ident() != self._thread:
            raise RuntimeError("Store is single-writer: use it from the thread that opened it")

    @contextlib.contextmanager
    def tx(self):
        """A transaction. Re-entrant: a `with store.tx()` inside another one joins it, so the
        engine can make "record the trigger, decide, open the ticket, queue the input" one
        commit; any exception rolls the whole outer transaction back. If an inner transaction
        fails and the caller catches the exception, the outer one still cannot commit: it rolls
        back and raises RuntimeError, instead of keeping half of what was done."""
        self._check()
        if self._depth:
            self._depth += 1
            try:
                yield self.db
            except BaseException:
                self._failed = True
                raise
            finally:
                self._depth -= 1
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._depth, self._failed = 1, False
        try:
            yield self.db
        except BaseException:
            self._depth, self._failed = 0, False
            self.db.execute("ROLLBACK")
            raise
        else:
            failed, self._depth, self._failed = self._failed, 0, False
            if failed:
                self.db.execute("ROLLBACK")
                raise RuntimeError("an inner transaction failed, so the whole transaction was rolled back")
            self.db.execute("COMMIT")

    def _q(self, sql, args=()):
        self._check()
        return _row(self.db.execute(sql, args))

    # ---------------------------------------------------------------- triggers, decisions
    def add_trigger(self, t):
        """-> the new trigger id, or None when this (source, ext_id) was seen before."""
        with self.tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO trigger(source, ext_id, thread, sender, text, addressed, ts) "
                "VALUES(?,?,?,?,?,?,?)",
                (t.source, t.ext_id, t.thread, t.sender, t.text, int(t.addressed), t.ts or time.time()))
            return cur.lastrowid if cur.rowcount else None

    def get_trigger(self, trigger_id):
        rows = self._q("SELECT * FROM trigger WHERE id=?", (trigger_id,))
        return rows[0] if rows else None

    def record_decision(self, trigger_id, d, ticket_id=None):
        with self.tx() as db:
            db.execute("INSERT INTO decision(trigger_id, verdict, strength, reason, would_accept, "
                       "ticket_id, ts) VALUES(?,?,?,?,?,?,?)",
                       (trigger_id, d.verdict.value, d.strength.value, d.reason,
                        int(d.would_accept), ticket_id, time.time()))

    def decisions(self):
        return self._q("SELECT * FROM decision ORDER BY id")

    # ---------------------------------------------------------------- tickets, mandates
    def new_ticket(self, source, thread, strength, paths_fn):
        """Create a ticket and its immutable mandate in one transaction.
        paths_fn(ticket_id) -> (cwd, session_file, root_realpath). -> (ticket_id, Mandate)"""
        with self.tx() as db:
            cur = db.execute("INSERT INTO ticket(source, thread, state, created) VALUES(?,?,?,?)",
                             (source, thread, TicketState.OPEN.value, time.time()))
            tid = cur.lastrowid
            cwd, session, root = paths_fn(tid)
            db.execute("UPDATE ticket SET cwd=?, session=? WHERE id=?", (cwd, session, tid))
            m = Mandate(tid, root, strength, source, thread)
            body = {"ticket_id": m.ticket_id, "root": m.root, "strength": m.strength.value,
                    "source": m.source, "thread": m.thread, "tools_allowed": list(m.tools_allowed)}
            db.execute("INSERT INTO mandate(ticket_id, body) VALUES(?,?)", (tid, json.dumps(body)))
            return tid, m

    def mandate(self, ticket_id):
        rows = self._q("SELECT body FROM mandate WHERE ticket_id=?", (ticket_id,))
        return json.loads(rows[0]["body"]) if rows else None

    def ticket(self, ticket_id):
        rows = self._q("SELECT * FROM ticket WHERE id=?", (ticket_id,))
        return rows[0] if rows else None

    def tickets(self, states=None):
        rows = self._q("SELECT * FROM ticket ORDER BY id")
        return [r for r in rows if states is None or r["state"] in {s.value for s in states}]

    def live_tickets_on(self, source, thread):
        live = {TicketState.OPEN.value, TicketState.WAITING.value}
        return [r["id"] for r in self._q("SELECT id, state FROM ticket WHERE source=? AND thread=?",
                                         (source, thread)) if r["state"] in live]

    def set_ticket_state(self, ticket_id, state):
        with self.tx() as db:
            if self.ticket(ticket_id)["state"] == TicketState.STOPPED.value \
                    and state is not TicketState.STOPPED:
                return False                 # a person's stop is final
            db.execute("UPDATE ticket SET state=? WHERE id=?", (state.value, ticket_id))
            return True

    def set_ticket_note(self, ticket_id, note):
        with self.tx() as db:
            db.execute("UPDATE ticket SET note=? WHERE id=?", (note, ticket_id))

    # ---------------------------------------------------------------- the delivery ledger
    def enqueue_input(self, ticket_id, trigger_id, delivery_key):
        """-> input id, or None when this delivery key already exists."""
        with self.tx() as db:
            cur = db.execute("INSERT OR IGNORE INTO input(ticket_id, trigger_id, delivery_key, state, ts) "
                             "VALUES(?,?,?,?,?)", (ticket_id, trigger_id, delivery_key,
                                                   InputState.PENDING.value, time.time()))
            return cur.lastrowid if cur.rowcount else None

    def next_deliverable(self, ticket_id):
        """The oldest input that may be sent now, or None. Never while one is in flight: one
        outstanding input per ticket is what lets a turn_end be matched to it."""
        rows = self._q("SELECT * FROM input WHERE ticket_id=? AND state IN (?,?,?) ORDER BY id",
                       (ticket_id, InputState.PENDING.value, InputState.SENT.value,
                        InputState.REPLAY.value))
        if any(r["state"] == InputState.SENT.value for r in rows):
            return None
        return rows[0] if rows else None

    def in_flight(self, ticket_id):
        rows = self._q("SELECT * FROM input WHERE ticket_id=? AND state=? ORDER BY id",
                       (ticket_id, InputState.SENT.value))
        return rows[0] if rows else None

    def mark_input(self, input_id, state):
        with self.tx() as db:
            db.execute("UPDATE input SET state=? WHERE id=?", (state.value, input_id))

    def recover_inflight(self, ticket_id=None):
        """SENT -> REPLAY: the process that was handling it is gone, so it may or may not have
        been handled. -> the input ids changed."""
        sql = "SELECT id FROM input WHERE state=?" + (" AND ticket_id=?" if ticket_id else "")
        args = (InputState.SENT.value,) + ((ticket_id,) if ticket_id else ())
        ids = [r["id"] for r in self._q(sql, args)]
        with self.tx() as db:
            for i in ids:
                db.execute("UPDATE input SET state=?, replayed=1 WHERE id=?",
                           (InputState.REPLAY.value, i))
        return ids

    def inputs(self, ticket_id):
        return self._q("SELECT * FROM input WHERE ticket_id=? ORDER BY id", (ticket_id,))

    # ---------------------------------------------------------------- processes and questions
    def start_run(self, ticket_id, pid, create_time, cwd):
        """-> the new run generation of this ticket (1, 2, ...)."""
        with self.tx() as db:
            db.execute("UPDATE ticket SET run_gen = run_gen + 1 WHERE id=?", (ticket_id,))
            gen = self.ticket(ticket_id)["run_gen"]
            db.execute("INSERT OR REPLACE INTO process(pid, create_time, ticket_id, cwd, run_gen, started) "
                       "VALUES(?,?,?,?,?,?)", (pid, create_time, ticket_id, cwd, gen, time.time()))
            return gen

    def end_run(self, ticket_id, pid):
        """The process is gone: forget it. No question it asked can be answered any more, and an
        answer it never showed it had received (see confirm_answers) is not reported as given.
        -> ids of the answers that were not confirmed."""
        with self.tx() as db:
            db.execute("DELETE FROM process WHERE pid=? AND ticket_id=?", (pid, ticket_id))
            lost = [r["ask_id"] for r in self._q(
                "SELECT ask_id FROM ask WHERE ticket_id=? AND state=? AND confirmed=0",
                (ticket_id, AskState.ANSWERED.value))]
            db.execute("UPDATE ask SET state=? WHERE ticket_id=? AND (state=? OR (state=? AND confirmed=0))",
                       (AskState.ORPHANED.value, ticket_id, AskState.OPEN.value, AskState.ANSWERED.value))
            return lost

    def confirm_answers(self, ticket_id):
        """The harness has shown progress (a tool result, the end of a turn, a new question) after
        an answer was written to it, so the answer was received."""
        with self.tx() as db:
            db.execute("UPDATE ask SET confirmed=1 WHERE ticket_id=? AND state=? AND confirmed=0",
                       (ticket_id, AskState.ANSWERED.value))

    def processes(self):
        return self._q("SELECT * FROM process ORDER BY started")

    def add_ask(self, ticket_id, run_gen, rid, what):
        with self.tx() as db:
            cur = db.execute("INSERT INTO ask(ticket_id, run_gen, rid, what, what_sha, state, ts) "
                             "VALUES(?,?,?,?,?,?,?)",
                             (ticket_id, run_gen, str(rid), what,
                              hashlib.sha256(what.encode("utf-8")).hexdigest(),
                              AskState.OPEN.value, time.time()))
            return cur.lastrowid

    def ask(self, ask_id):
        rows = self._q("SELECT * FROM ask WHERE ask_id=?", (ask_id,))
        return rows[0] if rows else None

    def open_asks(self, ticket_id=None):
        rows = self._q("SELECT * FROM ask WHERE state=? ORDER BY ask_id", (AskState.OPEN.value,))
        return [r for r in rows if ticket_id is None or r["ticket_id"] == ticket_id]

    def answerable(self, ask_id):
        """The ask row, if it is still open AND belongs to the ticket's current run; else
        AskRejected (a late answer after a restart can never reach a new question that happens
        to reuse the same rid). Changes nothing."""
        a = self.ask(ask_id)
        if a is None:
            raise AskRejected(f"no question {ask_id}")
        if a["state"] != AskState.OPEN.value:
            raise AskRejected(f"question {ask_id} is {a['state']}")
        if self.ticket(a["ticket_id"])["run_gen"] != a["run_gen"]:
            raise AskRejected(f"question {ask_id} belongs to an earlier run")
        return a

    def mark_answered(self, ask_id):
        with self.tx() as db:
            db.execute("UPDATE ask SET state=? WHERE ask_id=?", (AskState.ANSWERED.value, ask_id))

    def claim_answer(self, ask_id):
        """answerable + mark_answered in one step. -> the ask row."""
        with self.tx():
            a = self.answerable(ask_id)
            self.mark_answered(ask_id)
            return a

    # ---------------------------------------------------------------- outbox
    def outbox_enqueue(self, ticket_id, payload):
        """-> (row id, msg_id). msg_id is stable: it goes into the file name and the content."""
        msg_id = uuid.uuid4().hex
        with self.tx() as db:
            cur = db.execute("INSERT INTO outbox(ticket_id, msg_id, payload, status, ts) VALUES(?,?,?,?,?)",
                             (ticket_id, msg_id, json.dumps(payload), "pending", time.time()))
            return cur.lastrowid, msg_id

    def outbox_rows(self, status=None):
        rows = self._q("SELECT * FROM outbox ORDER BY id")
        return [r for r in rows if status is None or r["status"] == status]

    def outbox_mark(self, row_id, status, attempts=None):
        with self.tx() as db:
            if attempts is None:
                db.execute("UPDATE outbox SET status=? WHERE id=?", (status, row_id))
            else:
                db.execute("UPDATE outbox SET status=?, attempts=? WHERE id=?", (status, attempts, row_id))
