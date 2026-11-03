"""The orchestrator: one thread that owns the Store, polls the Source, decides, delivers, and
reacts to what the executors report.

Rules it keeps (each has a test):
  * a trigger enters the database before anything acts on it, and is idempotent;
  * a follow-up on a thread with a live ticket is delivered to it, not re-evaluated;
  * one input in flight per ticket; it is committed as SENT before it is written;
  * a question from a harness is never answered by this code, only by answer_ask() on a
    person's behalf, and only yes/no;
  * a ticket's executor is started only after preflight, with a mandate the hook can read;
  * after a crash, an input that was in flight is replayed with a warning, not guessed about.
"""
import json
import math
import os
import time

from . import executor as ex
from . import frame, gate, procutil
from . import main_session as ms
from .model import AskState, Decision, InputState, Strength, TicketState, Trigger, Verdict
from .outbox import Outbox
from .store import AskRejected, StaleBatch
from .turnreply import ReplyTracker, reply_payload

MAX_QUICK_DEATHS = 3          # an executor that dies this many times without finishing a turn is parked
QUICK = 30.0                  # seconds: "quick" for the above


class _Run:
    """One live executor process of one ticket."""

    def __init__(self, ticket_id, executor, run_gen, pid):
        self.ticket_id, self.executor, self.run_gen, self.pid = ticket_id, executor, run_gen, pid
        self.started = time.time()
        self.turns = 0
        self.tracker = ReplyTracker()     # what this process says at the end of each turn
        self.confirm_after = 0     # events numbered up to this were queued before the last answer was written: not its consequence


class Engine:
    def __init__(self, store, source, sink, policy, assessor, executor_factory, *,
                 ask_timeout=3600.0, max_running=4, clock=time.time,
                 main=None, batch_max=ms.BATCH_MAX, batch_min=1, batch_wait=3.0, eval_timeout=180.0,
                 card_cap=ms.CARD_CAP, idle_end=600.0):
        """main: a MainSession turns on model assessment of inferred candidates (otherwise the
        rule-based assessor decides them, exactly as in Slice 1). idle_end: seconds after its last
        activity at which a ticket with nothing left to do is ended and its process stopped."""
        # `type(...) is int` keeps out True/False; math.isfinite keeps out nan (every comparison with it
        # is false, so `nan <= 0` would pass and `elapsed > nan` would never time out) and infinity.
        whole = lambda v: type(v) is int                                          # noqa: E731
        number = lambda v: type(v) in (int, float) and math.isfinite(v)           # noqa: E731
        if not (number(idle_end) and idle_end > 0):
            raise ValueError("idle_end must be a finite number of seconds above 0")
        if main is not None:
            if not (whole(batch_max) and 1 <= batch_max <= 100):
                raise ValueError("batch_max must be a whole number from 1 to 100 (it bounds how much one "
                                 "poisoned batch can do)")
            if not (whole(batch_min) and 1 <= batch_min <= batch_max):
                raise ValueError("batch_min must be a whole number from 1 up to batch_max")
            if not (number(batch_wait) and batch_wait >= 0 and number(eval_timeout) and eval_timeout > 0
                    and whole(card_cap) and card_cap >= 1):
                raise ValueError("batch_wait must be a finite number, 0 or more; eval_timeout a finite number "
                                 "above 0; card_cap a whole number, 1 or more")
        self.store, self.source, self.policy, self.assessor = store, source, policy, assessor
        self.outbox = Outbox(store, sink)
        self.executor_factory = executor_factory
        self.home = store.home
        self.ask_timeout, self.max_running, self.clock = ask_timeout, max_running, clock
        self.idle_end = idle_end
        self.runs = {}                 # ticket_id -> _Run
        self.deaths = {}               # ticket_id -> consecutive quick deaths
        self.events = []               # what happened, for status and tests (bounded)
        self.fault = lambda point: None    # tests raise from here to simulate a crash at a point
        self.main = main
        self.batch_max, self.batch_min, self.batch_wait = batch_max, batch_min, batch_wait
        self.eval_timeout, self.card_cap = eval_timeout, card_cap
        self.main_retry_at = 0.0
        if main is not None:                # the model session is a process like any other: record it
            main.on_start = lambda pid, ctime, cwd: self.store.record_process(pid, ctime, 0, cwd)
            main.on_stop = lambda pid: self.store.forget_process(pid, 0)

    # ------------------------------------------------------------------ helpers
    def note(self, kind, **data):
        self.events.append(dict(data, kind=kind, ts=self.clock()))
        del self.events[:-1000]

    def paths(self, ticket_id):
        base = os.path.join(self.home, "tickets", str(ticket_id))
        cwd = os.path.join(base, "work")
        os.makedirs(cwd, exist_ok=True)
        return cwd, os.path.join(base, "session.json"), os.path.realpath(cwd)

    # ------------------------------------------------------------------ start / recovery
    def recover(self):
        """Run once at startup. A previous orchestrator that died may have left harness
        processes running; they would fight the new ones over the same session files, so any
        process we recorded and can positively identify is killed. Inputs that were in flight
        become REPLAY."""
        for row in self.store.processes():
            if procutil.is_ours(row["pid"], row["create_time"], self.home):
                ex.kill_pid(row["pid"])
                self.note("killed_orphan", pid=row["pid"], ticket=row["ticket_id"])
            self.store.end_run(row["ticket_id"], row["pid"])
        self.store.recover_inflight()
        for b in self.store.sent_batches():       # the model's answer was never applied: ask again
            self.store.requeue_eval(b["id"], "orchestrator restarted", count=False)
        for t in self.store.tickets({TicketState.WAITING}):
            if not t["note"] and not self.store.open_asks(t["id"]):
                self.store.set_ticket_state(t["id"], TicketState.OPEN)

    # ------------------------------------------------------------------ one pass
    def tick(self):
        for t in self.source.poll():
            with self.store.tx():              # the trigger and everything it causes: one commit
                trid = self.store.add_trigger(t)
                if trid is not None:
                    self.handle_trigger(t, trid)
            self.source.settle(t)              # only after the commit: a crash before it re-reads the file
        for tid in [t["id"] for t in self.store.tickets({TicketState.OPEN, TicketState.WAITING})]:
            self.drive(tid)
        self.end_idle()
        self.expire_asks()
        self.eval_stage()
        self.outbox.flush()

    def handle_trigger(self, t, trigger_id):
        live = self.store.live_tickets_on(t.source, t.thread)
        if live:                                     # a follow-up: no Eval, but it must be admitted
            strength = gate.admit(t, self.policy, True)
            if strength is not Strength.EXPLICIT:
                # Only a person on the allow list can steer a ticket. A stranger's words (or an
                # inferred sender's, or anyone's while the accept mode is `off`) are not delivered.
                self.store.record_decision(trigger_id, Decision(
                    Verdict.IGNORE, strength, f"follow-up not admitted ({strength.value}): nothing was delivered"))
                self.note("follow_up_not_admitted", trigger=trigger_id, sender=t.sender)
                return
            for tid in live:
                self.store.enqueue_input(tid, trigger_id, f"{tid}:{trigger_id}", self.clock())
            self.store.record_decision(
                trigger_id, Decision(Verdict.FANOUT, strength, f"delivered to ticket(s) {live}"), live[0])
            return
        ended = self.store.ended_tickets_on(t.source, t.thread)
        if ended and gate.admit(t, self.policy, True) is Strength.EXPLICIT:
            # A person on the allow list speaks on a thread whose ticket had ended: the same ticket
            # (same session, same mandate) opens again. Admission is decided now, by the current
            # policy, for this message; the old mandate only says what the ticket may do.
            tid = ended[0]
            self.store.reopen_ticket(tid, trigger_id, f"{tid}:{trigger_id}", self.clock())
            self.store.record_decision(trigger_id, Decision(
                Verdict.REOPEN, Strength.EXPLICIT, f"reopened ticket {tid}"), tid)
            self.outbox.enqueue(tid, f"Ticket {tid} reopened for this message.")
            self.note("ticket_reopened", ticket=tid)
            return
        if self.main is not None and gate.admit(t, self.policy, False) is Strength.INFERRED:
            self.store.enqueue_eval(trigger_id, self.clock())       # the model decides; no decision yet
            self.note("queued_for_model", trigger=trigger_id)
            return
        d = gate.evaluate(t, self.policy, self.assessor, False)
        if d.verdict is Verdict.ACCEPT:
            tid, _ = self.store.new_ticket(t.source, t.thread, d.strength, self.paths, now=self.clock())
            self.fault("in_trigger")
            self.store.record_decision(trigger_id, d, tid)
            self.store.enqueue_input(tid, trigger_id, f"{tid}:{trigger_id}", self.clock())
            self.outbox.enqueue(tid, f"Ticket {tid} opened for this request.")
            self.note("ticket_opened", ticket=tid)
        else:
            self.store.record_decision(trigger_id, d)

    # ------------------------------------------------------------------ the model's share
    def eval_stage(self):
        """Hand pending candidates to the model session in batches, and apply its answers. Never waits."""
        if self.main is None:
            return
        sent = self.store.sent_batch()
        if sent is not None and self.clock() - sent["sent_at"] > self.eval_timeout:
            # Past the deadline: give up BEFORE looking at anything the model sent, so a late answer
            # that happens to be waiting in the queue cannot open a ticket either.
            self.note("eval_timeout", batch=sent["id"])
            self.main.stop()
            self.store.requeue_eval(sent["id"], "timeout")
            return
        for ev in self.main.poll():
            self.on_main_event(ev)
        if self.store.sent_batch() is not None:
            return
        items = self.store.peek_eval_items(self.batch_max)
        if not items or self.clock() < self.main_retry_at:
            return
        if len(items) < self.batch_min and self.clock() - items[0]["ts"] < self.batch_wait:
            return
        if not self.main.ensure_running():
            self.note("main_cannot_start", why=self.main.last_error)
            self.main_retry_at = self.clock() + 30.0
            return
        text = ms.build_batch_text(items, self.policy.rules, self.card_cap)
        batch_id = self.store.create_eval_batch([i["trigger_id"] for i in items], text,
                                                self.main.generation, self.clock())
        self.fault("after_batch_commit")
        if not self.main.submit(batch_id, text):
            self.store.requeue_eval(batch_id, "the model session is not running")

    def on_main_event(self, ev):
        batch = self.store.eval_batch(ev.batch_id)
        if batch is None or batch["state"] != "sent":
            return                                                  # an answer to a batch that is no longer in flight
        if ev.kind == "failed":
            self.store.requeue_eval(ev.batch_id, ev.text)
            self.note("batch_failed", batch=ev.batch_id, why=ev.text)
            return
        ids = self.store.eval_batch_items(ev.batch_id)
        self.note("model_reply", batch=ev.batch_id, text=ev.text[:2000])
        parsed = ms.parse_reply(ev.text, ids)
        if not parsed.valid_json:
            self.store.requeue_eval(ev.batch_id, "unreadable reply")
            self.note("batch_failed", batch=ev.batch_id, why="unreadable reply", errors=parsed.errors)
            return
        try:
            report = self.store.finish_eval(ev.batch_id, parsed.ok, self.policy, self.paths, self._opened_by_model)
        except StaleBatch as e:
            self.note("batch_stale", batch=ev.batch_id, why=str(e))
            return
        self.note("batch_done", batch=ev.batch_id, **report)

    def _opened_by_model(self, trig, ticket_id):
        self.store.enqueue_input(ticket_id, trig["id"], f"{ticket_id}:{trig['id']}", self.clock())
        self.outbox.enqueue(ticket_id, f"Ticket {ticket_id} opened for this request.")
        self.note("ticket_opened", ticket=ticket_id, by="model")

    def drive(self, ticket_id):
        run = self.runs.get(ticket_id)
        if run is not None:
            self.pump(run)
            run = self.runs.get(ticket_id)           # pump may have ended it
        ticket = self.store.ticket(ticket_id)
        if ticket["state"] in (TicketState.STOPPED.value, TicketState.ENDED.value):
            return
        if ticket["state"] == TicketState.WAITING.value and not self.store.open_asks(ticket_id) \
                and not ticket["note"] and run is not None:
            self.store.set_ticket_state(ticket_id, TicketState.OPEN)
        nxt = self.store.next_deliverable(ticket_id)
        if nxt is None or ticket["note"]:
            return
        if run is None:
            run = self.launch(ticket_id)
            if run is None:
                return
        if not run.executor.alive:
            return
        self.deliver(run, nxt)

    # ------------------------------------------------------------------ executors
    def launch(self, ticket_id):
        if len(self.runs) >= self.max_running:
            return None
        ticket = self.store.ticket(ticket_id)
        base = os.path.dirname(ticket["session"])
        mandate_path = os.path.join(base, "mandate.json")
        executor = self.executor_factory(ticket_id)
        try:
            with open(mandate_path, "w", encoding="utf-8") as f:
                json.dump(self.store.mandate(ticket_id), f)
            ex.write_ticket_files(ticket["cwd"], mandate_path)
            executor.start(ticket["cwd"], ticket["session"])
        except (ex.PreflightError, OSError) as e:
            self.park(ticket_id, f"cannot start: {e}")
            return None
        gen = self.store.start_run(ticket_id, executor.pid, executor.create_time or 0.0, ticket["cwd"])
        run = _Run(ticket_id, executor, gen, executor.pid)
        self.runs[ticket_id] = run
        self.note("started", ticket=ticket_id, pid=run.pid, run_gen=gen)
        return run

    def park(self, ticket_id, why):
        """Fail closed and visibly: the ticket waits for a person, with the reason attached."""
        self.store.set_ticket_note(ticket_id, why)
        self.store.set_ticket_state(ticket_id, TicketState.WAITING)
        self.outbox.enqueue(ticket_id, f"Ticket {ticket_id} needs attention: {why}")
        self.note("parked", ticket=ticket_id, why=why)

    def resume(self, ticket_id):
        """A person has dealt with whatever parked the ticket."""
        self.store.set_ticket_note(ticket_id, None)
        self.deaths.pop(ticket_id, None)
        self.store.set_ticket_state(ticket_id, TicketState.OPEN)

    def deliver(self, run, inp):
        trig = self.store.get_trigger(inp["trigger_id"])
        text = frame.wrap_external(trig["source"], trig["thread"], trig["sender"], trig["text"],
                                   inp["id"], replay=inp["state"] == InputState.REPLAY.value)
        self.store.mark_input(inp["id"], InputState.SENT)          # committed first ...
        self.fault("after_sent_commit")
        if not run.executor.send(text):                             # ... then written
            self.store.mark_input(inp["id"], InputState.PENDING)

    def pump(self, run):
        """Handle everything this executor has reported since the last pass."""
        q = run.executor.events
        while True:
            try:
                ev = q.get_nowait()
            except Exception:
                break
            stale = run.confirm_after > 0 and ev.get("_seq", 0) <= run.confirm_after
            kind = ev.get("kind")
            if ev.get("_pid") not in (None, run.pid):
                continue                                            # a late event of an older process
            ended = run.tracker.feed(ev)        # after the check above: an older process must not touch it
            if kind in ("tool_result", "turn_end", "ask") and not stale:
                self.store.confirm_answers(run.ticket_id)          # progress AFTER the answer proves it arrived
            if kind == "turn_end":
                run.turns += 1
                self.deaths.pop(run.ticket_id, None)
                self.finish_turn(run, ended)
            elif kind == "ask":
                self.on_ask(run, ev)
            elif kind == "log" and "could not write" in str(ev.get("text", "")):
                self.note("checkpoint_warning", ticket=run.ticket_id, text=ev["text"])
            elif kind == "exited":
                self.on_exit(run, ev)
                return

    def finish_turn(self, run, ended):
        """A turn is over: its input is done and its reply (or the notice that there was none) is
        queued, in one transaction."""
        flight = self.store.in_flight(run.ticket_id)
        if flight is None:
            self.note("turn_end_without_input", ticket=run.ticket_id)
            return
        payload = reply_payload(run.ticket_id, flight["id"], ended.text)
        if not self.store.finish_turn(run.ticket_id, flight["id"], payload, self.clock(),
                                      fault=lambda: self.fault("in_finish_turn")):
            self.note("turn_not_finished", ticket=run.ticket_id, input=flight["id"])
        elif payload["kind"] == "status":
            self.note("turn_without_reply", ticket=run.ticket_id)

    def on_ask(self, run, ev):
        if ev.get("question"):                                      # ask_user: nobody types free text
            run.executor.answer_empty(ev["rid"])
            self.note("question_declined", ticket=run.ticket_id, what=ev.get("what"))
            return
        with self.store.tx():
            ask_id = self.store.add_ask(run.ticket_id, run.run_gen, ev["rid"], ev.get("what", ""))
            self.store.set_ticket_state(run.ticket_id, TicketState.WAITING)
            self.outbox.enqueue(run.ticket_id,
                                f"Ticket {run.ticket_id} asks (ask {ask_id}): {ev.get('what', '')}  "
                                f"Answer with: answer {ask_id} y|n")
        self.note("ask", ticket=run.ticket_id, ask_id=ask_id)

    def on_exit(self, run, ev):
        tid = run.ticket_id
        self.runs.pop(tid, None)
        run.executor.stop(force=True)
        lost = self.store.end_run(tid, run.pid)
        replayed = self.store.recover_inflight(tid)
        self.note("exited", ticket=tid, code=ev.get("code"), replay=replayed, unconfirmed_answers=lost)
        if self.store.ticket(tid)["state"] == TicketState.WAITING.value and not self.store.ticket(tid)["note"]:
            self.store.set_ticket_state(tid, TicketState.OPEN)
        if self.clock() - run.started < QUICK and run.turns == 0:
            self.deaths[tid] = self.deaths.get(tid, 0) + 1
            if self.deaths[tid] >= MAX_QUICK_DEATHS:
                self.park(tid, f"its executor died {self.deaths[tid]} times without finishing a turn")

    # ------------------------------------------------------------------ people
    def answer_ask(self, ask_id, yes):
        """The only way a question gets an answer. Raises AskRejected for anything that is not
        a live question of the ticket's current process."""
        row = self.store.answerable(ask_id)
        run = self.runs.get(row["ticket_id"])
        if run is None or run.run_gen != row["run_gen"] or not run.executor.alive:
            raise AskRejected(f"question {ask_id} has no live process")
        if not run.executor.answer(row["rid"], bool(yes)):       # delivered, or it stays open
            raise AskRejected(f"question {ask_id}: the answer could not be delivered; try again")
        run.confirm_after = max(run.confirm_after, run.executor.last_answer_mark)
        self.store.mark_answered(ask_id, self.clock())
        self.note("answered", ticket=row["ticket_id"], ask_id=ask_id, yes=bool(yes))

    def expire_asks(self):
        for a in self.store.open_asks():
            if self.clock() - a["ts"] >= self.ask_timeout:
                try:
                    self.answer_ask(a["ask_id"], False)
                    self.note("ask_timed_out", ask_id=a["ask_id"])
                except AskRejected:
                    pass

    def end_idle(self):
        """A ticket that has nothing left to do ends, and its process goes: otherwise a finished
        ticket keeps its slot for ever. The decision is made in the database (one transaction that
        re-checks everything); the process is stopped only after it has been committed."""
        for t in self.store.tickets({TicketState.OPEN}):
            if self.store.end_ticket_if_idle(t["id"], self.clock(), self.idle_end):
                self.note("ticket_ended", ticket=t["id"])
                self.fault("after_ended_commit")          # a crash here leaves an ended ticket with a live process
                self.release(t["id"])
        for tid in list(self.runs):                         # ... and the next pass (or the next start) tidies it
            if self.store.ticket(tid)["state"] == TicketState.ENDED.value:
                self.release(tid)

    def release(self, ticket_id):
        """Stop this ticket's process (if it has one) and give its slot back."""
        run = self.runs.pop(ticket_id, None)
        if run is not None:
            run.executor.stop()
            self.store.end_run(ticket_id, run.pid)

    def stop_ticket(self, ticket_id):
        """A person's stop is final: the process ends and nothing more is delivered."""
        self.store.set_ticket_state(ticket_id, TicketState.STOPPED)
        run = self.runs.pop(ticket_id, None)
        if run is not None:
            run.executor.stop(force=True)
            self.store.end_run(ticket_id, run.pid)
        self.note("stopped", ticket=ticket_id)

    def shutdown(self):
        """Stop every executor; inputs in flight become REPLAY so a restart repeats them."""
        for tid in list(self.runs):
            run = self.runs.pop(tid)
            run.executor.stop()
            self.store.end_run(tid, run.pid)
        self.store.recover_inflight()
        if self.main is not None:
            self.main.stop()
            for b in self.store.sent_batches():
                self.store.requeue_eval(b["id"], "orchestrator stopped", count=False)

    # ------------------------------------------------------------------ views
    def status(self):
        return {
            "tickets": [{"id": t["id"], "state": t["state"], "thread": t["thread"], "note": t["note"],
                         "last_activity": t["last_activity"],
                         "pid": self.runs[t["id"]].pid if t["id"] in self.runs else None,
                         "pending_inputs": len([i for i in self.store.inputs(t["id"])
                                                if i["state"] != InputState.DONE.value])}
                        for t in self.store.tickets()],
            "open_asks": [{"ask_id": a["ask_id"], "ticket": a["ticket_id"], "what": a["what"]}
                          for a in self.store.open_asks()],
            "outbox": {s: len(self.store.outbox_rows(s)) for s in ("pending", "sent", "failed")},
            "eval": self.store.eval_counts(),
            "model_session": None if self.main is None else {
                "generation": self.main.generation, "alive": self.main.alive, "busy": self.main.busy,
                "last_error": self.main.last_error},
        }
