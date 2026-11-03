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
import os
import time

from . import executor as ex
from . import frame, gate, procutil
from .model import AskState, Decision, InputState, Strength, TicketState, Trigger, Verdict
from .outbox import Outbox
from .store import AskRejected

MAX_QUICK_DEATHS = 3          # an executor that dies this many times without finishing a turn is parked
QUICK = 30.0                  # seconds: "quick" for the above


class _Run:
    """One live executor process of one ticket."""

    def __init__(self, ticket_id, executor, run_gen, pid):
        self.ticket_id, self.executor, self.run_gen, self.pid = ticket_id, executor, run_gen, pid
        self.started = time.time()
        self.turns = 0


class Engine:
    def __init__(self, store, source, sink, policy, assessor, executor_factory, *,
                 ask_timeout=3600.0, max_running=4, clock=time.time):
        self.store, self.source, self.policy, self.assessor = store, source, policy, assessor
        self.outbox = Outbox(store, sink)
        self.executor_factory = executor_factory
        self.home = store.home
        self.ask_timeout, self.max_running, self.clock = ask_timeout, max_running, clock
        self.runs = {}                 # ticket_id -> _Run
        self.deaths = {}               # ticket_id -> consecutive quick deaths
        self.events = []               # what happened, for status and tests (bounded)
        self.fault = lambda point: None    # tests raise from here to simulate a crash at a point

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
        self.expire_asks()
        self.outbox.flush()

    def handle_trigger(self, t, trigger_id):
        live = self.store.live_tickets_on(t.source, t.thread)
        if live:                                     # a follow-up: no Eval, delivered as context
            for tid in live:
                self.store.enqueue_input(tid, trigger_id, f"{tid}:{trigger_id}")
            self.store.record_decision(
                trigger_id, Decision(Verdict.FANOUT, gate.admit(t, self.policy, True),
                                     f"delivered to ticket(s) {live}"), live[0])
            return
        d = gate.evaluate(t, self.policy, self.assessor, False)
        if d.verdict is Verdict.ACCEPT:
            tid, _ = self.store.new_ticket(t.source, t.thread, d.strength, self.paths)
            self.fault("in_trigger")
            self.store.record_decision(trigger_id, d, tid)
            self.store.enqueue_input(tid, trigger_id, f"{tid}:{trigger_id}")
            self.outbox.enqueue(tid, f"Ticket {tid} opened for this request.")
            self.note("ticket_opened", ticket=tid)
        else:
            self.store.record_decision(trigger_id, d)

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
            kind = ev.get("kind")
            if ev.get("_pid") not in (None, run.pid):
                continue                                            # a late event of an older process
            if kind == "turn_end":
                run.turns += 1
                self.deaths.pop(run.ticket_id, None)
                flight = self.store.in_flight(run.ticket_id)
                if flight:
                    self.store.mark_input(flight["id"], InputState.DONE)
            elif kind == "ask":
                self.on_ask(run, ev)
            elif kind == "log" and "could not write" in str(ev.get("text", "")):
                self.note("checkpoint_warning", ticket=run.ticket_id, text=ev["text"])
            elif kind == "exited":
                self.on_exit(run, ev)
                return

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
        self.store.end_run(tid, run.pid)
        replayed = self.store.recover_inflight(tid)
        self.note("exited", ticket=tid, code=ev.get("code"), replay=replayed)
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
        self.store.mark_answered(ask_id)
        self.note("answered", ticket=row["ticket_id"], ask_id=ask_id, yes=bool(yes))

    def expire_asks(self):
        for a in self.store.open_asks():
            if self.clock() - a["ts"] >= self.ask_timeout:
                try:
                    self.answer_ask(a["ask_id"], False)
                    self.note("ask_timed_out", ask_id=a["ask_id"])
                except AskRejected:
                    pass

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

    # ------------------------------------------------------------------ views
    def status(self):
        return {
            "tickets": [{"id": t["id"], "state": t["state"], "thread": t["thread"], "note": t["note"],
                         "pid": self.runs[t["id"]].pid if t["id"] in self.runs else None,
                         "pending_inputs": len([i for i in self.store.inputs(t["id"])
                                                if i["state"] != InputState.DONE.value])}
                        for t in self.store.tickets()],
            "open_asks": [{"ask_id": a["ask_id"], "ticket": a["ticket_id"], "what": a["what"]}
                          for a in self.store.open_asks()],
            "outbox": {s: len(self.store.outbox_rows(s)) for s in ("pending", "sent", "failed")},
        }
