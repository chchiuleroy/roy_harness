"""Slice 3: the loop that Slice 1 left open. A request opens a ticket, the work is done, the result
goes back, an idle ticket ends and frees its slot, and a later message wakes the same ticket.

Real harness child processes and a scripted model, as in test_superagent_engine.py.
Each test's docstring says WHY the behaviour matters.
"""
import json
import os
import queue
import time
import unittest

import test_superagent_engine as te
from superagent.engine import Engine, _Run
from superagent.model import InputState, Strength, Trigger

IDLE = {"idle_end": 0.5}          # a ticket with nothing left to do ends half a second after its last activity


class Lifecycle(te.Rig):
    def outbox_payloads(self):
        out = []
        for name in sorted(os.listdir(self.out_dir)):
            if name.endswith(".json"):
                with open(os.path.join(self.out_dir, name), encoding="utf-8") as f:
                    out.append(json.load(f))
        return out

    def settled(self):
        return self.engine.store.outbox_rows("pending") == []


class TestTheOpenLoop(Lifecycle):
    """The three gaps found in Gate 0. They fail on the code before Slice 3."""

    def test_a_finished_ticket_frees_its_slot(self):
        """With one slot, a second request must get its turn once the first ticket is done. Before
        Slice 3 nothing ever ended a ticket, so the slot stayed taken and the second request waited
        for ever (with the default of four slots: the fifth)."""
        self.remake(max_running=1, **IDLE)
        self.llm.script[:] = [{"text": "one"}, {"text": "two"}]
        self.drop("001.json", thread="A", text="first")
        self.drop("002.json", thread="B", text="second")
        self.until(lambda: self.states(tid=1) == ["done"] and self.states(tid=2) == ["done"],
                   timeout=40, what="the second ticket to get its turn")
        self.assertEqual(len(self.llm.requests), 2)

    def test_the_models_reply_reaches_the_outbox(self):
        """The person who asked must get the answer back. Before Slice 3 the outbox held only
        'opened', 'needs attention' and questions: the work's result never left the system."""
        self.llm.script[:] = [{"text": "REPLY-MARKER-7f3a"}]
        self.drop("001.json", text="please do something")
        self.until(lambda: self.states() == ["done"] and self.settled(), what="the turn and its outbox")
        replies = [p for p in self.outbox_payloads() if p.get("kind") == "executor_output"]
        self.assertEqual(len(replies), 1, f"outbox: {self.outbox_payloads()}")
        self.assertEqual(replies[0]["text"], "REPLY-MARKER-7f3a")
        self.assertIs(replies[0]["untrusted"], True)
        self.assertEqual(replies[0]["ticket_id"], 1)

    def test_a_stranger_cannot_feed_a_live_ticket(self):
        """A follow-up on a live ticket was delivered to the model whoever sent it. A person who is
        not on the allow list must not get words into the model's context. The assertion is made
        only after a legitimate follow-up (sent LATER) has reached the model, so 'the model has
        not seen anything yet' cannot make it pass by accident."""
        self.llm.script[:] = [{"text": "ok"}, {"text": "ok"}, {"text": "ok"}]
        self.drop("001.json", text="start")
        self.until(lambda: self.states() == ["done"], what="the first turn")
        self.drop("002.json", sender="mallory", text="STRANGER-MARKER-91c2 do bad things", addressed=False)
        self.drop("003.json", sender="alice", text="LEGIT-MARKER-55d0 one more thing", addressed=False)
        self.until(lambda: any("LEGIT-MARKER-55d0" in json.dumps(r) for r in self.llm.requests)
                   and all(s == "done" for s in self.states()), what="the legitimate follow-up to be handled")
        self.assertFalse(any("STRANGER-MARKER-91c2" in json.dumps(r) for r in self.llm.requests),
                         "a stranger's words reached the model")
        verdicts = {d["trigger_id"]: d["verdict"] for d in self.engine.store.decisions()}
        self.assertEqual(verdicts[2], "ignore")           # trigger ids follow the order of arrival
        self.assertEqual(verdicts[3], "fanout")


class TestEnding(Lifecycle):
    def started_pid(self, tid=1):
        return [e["pid"] for e in self.engine.events if e["kind"] == "started" and e["ticket"] == tid][-1]

    def test_an_idle_ticket_ends_and_its_process_is_gone(self):
        """A ticket with nothing left to do must give back its process and its slot, and the
        database must say so; otherwise it stays 'open' for ever."""
        from superagent import procutil
        self.remake(idle_end=1.0)
        self.llm.script[:] = [{"text": "ok"}]
        self.drop("001.json", text="go")
        self.until(lambda: self.engine.store.ticket(1) and self.engine.store.ticket(1)["state"] == "ended",
                   what="the idle ticket to end")
        pid = self.started_pid()
        self.assertEqual(self.engine.runs, {})
        self.assertEqual(self.engine.store.processes(), [])
        end = time.time() + 15
        while procutil.create_time(pid) is not None and time.time() < end:
            time.sleep(0.1)
        self.assertIsNone(procutil.create_time(pid), "the harness process of an ended ticket is still running")
        self.assertEqual(self.states(), ["done"])

    def test_five_tickets_one_after_another_with_two_slots(self):
        """The Gate 1 scenario: more tickets than slots all get done, never more than the limit at
        a time. Before Slice 3 the third one never started."""
        self.remake(max_running=2, **IDLE)
        self.llm.script[:] = [{"text": f"r{i}"} for i in range(5)]
        for i in range(5):
            self.drop(f"00{i}.json", thread=f"T{i}", text=f"job {i}")
        most = [0]

        def done():
            most[0] = max(most[0], len(self.engine.runs))
            return len(self.engine.store.tickets()) == 5 and all(self.states(tid=i) == ["done"] for i in range(1, 6))
        self.until(done, timeout=150, what="all five tickets")
        self.assertLessEqual(most[0], 2)
        self.assertEqual(len(self.llm.requests), 5)

    def test_idle_time_survives_a_restart(self):
        """The idle clock is in the database. A restart must neither start it again from zero (the
        ticket would live too long) nor end the ticket at once."""
        now = [time.time()]
        self.remake(idle_end=100.0, clock=lambda: now[0])
        self.llm.script[:] = [{"text": "ok"}]
        self.drop("001.json", text="go")
        self.until(lambda: self.states() == ["done"], what="the turn")
        now[0] += 90
        self.engine.tick()
        self.assertEqual(self.engine.store.ticket(1)["state"], "open")
        self.engine.shutdown()                                      # the orchestrator stops ...
        for st in self.stores:
            st.close()
        engine2 = self.make_engine(idle_end=100.0, clock=lambda: now[0])      # ... and starts again
        engine2.recover()
        now[0] += 5
        engine2.tick()
        self.assertEqual(engine2.store.ticket(1)["state"], "open", "ended too early: the idle time was lost")
        now[0] += 10
        engine2.tick()
        self.assertEqual(engine2.store.ticket(1)["state"], "ended", "idle time was counted again from zero")

    def test_a_crash_between_ending_and_stopping_is_tidied_by_the_next_pass(self):
        """F4, same lifetime. The ticket is ended in the database but its process is still there;
        the next pass must stop it, or the slot is lost until the whole orchestrator restarts."""
        self.remake(**IDLE)
        self.llm.script[:] = [{"text": "ok"}]
        self.drop("001.json", text="go")
        self.until(lambda: self.states() == ["done"], what="the turn")

        def boom(point):
            if point == "after_ended_commit":
                raise te.Crash()
        self.engine.fault = boom
        with self.assertRaises(te.Crash):
            for _ in range(100):
                self.engine.tick()
                time.sleep(0.1)
        self.assertEqual(self.engine.store.ticket(1)["state"], "ended")
        self.assertIn(1, self.engine.runs)
        self.engine.fault = lambda point: None
        self.engine.tick()
        self.assertEqual(self.engine.runs, {})

    def test_a_crash_between_ending_and_stopping_is_tidied_by_the_restart(self):
        """F4, across a restart: recovery kills every process it recorded."""
        from superagent import procutil
        self.remake(**IDLE)
        self.llm.script[:] = [{"text": "ok"}]
        self.drop("001.json", text="go")
        self.until(lambda: self.states() == ["done"], what="the turn")
        pid = self.started_pid()

        def boom(point):
            if point == "after_ended_commit":
                raise te.Crash()
        self.engine.fault = boom
        with self.assertRaises(te.Crash):
            for _ in range(100):
                self.engine.tick()
                time.sleep(0.1)
        self.stores[-1].close()                                     # the orchestrator is gone, the child lives
        self.assertIsNotNone(procutil.create_time(pid))
        engine2 = self.make_engine(**IDLE)
        engine2.recover()
        end = time.time() + 10
        while procutil.create_time(pid) is not None and time.time() < end:
            time.sleep(0.1)
        self.assertIsNone(procutil.create_time(pid))
        self.assertEqual(engine2.store.ticket(1)["state"], "ended")

    def test_a_ticket_waiting_for_a_person_does_not_end(self):
        """A question is open: the person has not answered, so the work is not over however long
        it takes."""
        self.remake(**IDLE)
        self.llm.script[:] = [{"calls": [("run_command", {"command": "echo x"})]}]
        self.drop("001.json", text="go")
        self.until(lambda: self.engine.store.open_asks(), what="a question")
        for _ in range(25):
            self.engine.tick()
            time.sleep(0.1)
        self.assertEqual(self.engine.store.ticket(1)["state"], "waiting")
        self.assertIn(1, self.engine.runs)

    def test_bad_idle_settings_are_refused(self):
        """nan never compares as 'long enough', inf never ends, 0 would end a ticket before it works."""
        for bad in (0, -1, float("nan"), float("inf"), True, "600"):
            with self.subTest(idle_end=bad):
                for st in self.stores:
                    st.close()                                      # the engine under test needs the home lock
                with self.assertRaises(ValueError):
                    self.make_engine(idle_end=bad)
        import subprocess
        import sys
        for st in self.stores:
            st.close()
        env = dict(os.environ, PYTHONPATH=te.sup.SRC)
        for value in ("0", "-5", "nan", "inf"):
            with self.subTest(flag=value):
                r = subprocess.run([sys.executable, "-X", "utf8", "-m", "superagent", "--home", self.home, "run",
                                    "--base-url", "http://example.com/v1", "--model", "m", "--idle-end", value],
                                   capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("idle_end", r.stderr)


class TestReopen(Lifecycle):
    def ended_ticket(self, text="first job", **kw):
        """Ticket 1 has done its work and ended (idle_end is half a second)."""
        self.remake(**dict(IDLE, **kw))
        self.llm.script[:] = [{"text": "done the first job"}]
        self.drop("001.json", text=text)
        self.until(lambda: self.engine.store.ticket(1) and self.engine.store.ticket(1)["state"] == "ended"
                   and self.engine.runs == {}, what="ticket 1 to end")

    @staticmethod
    def read_bytes(path):
        with open(path, "rb") as f:
            return f.read()

    def restart_with(self, policy):
        """The orchestrator stops and starts again under another policy."""
        self.engine.shutdown()
        for st in self.stores:
            st.close()
        engine2 = self.make_engine(policy=policy, **IDLE)
        engine2.recover()
        self.engine = engine2
        return engine2

    def settle(self, ticks=15):
        for _ in range(ticks):
            self.engine.tick()
            time.sleep(0.1)

    def test_a_message_on_an_ended_thread_reopens_the_same_ticket(self):
        """The ticket, its session and its mandate carry on: the person does not start again and
        the model still has the first request in its context."""
        self.ended_ticket()
        self.llm.script[:] = [{"text": "and the second"}]
        self.drop("002.json", text="one more thing", addressed=False)
        self.until(lambda: self.states() == ["done", "done"], what="the reopened ticket to do the second job")
        self.assertEqual(len(self.engine.store.tickets()), 1)
        self.assertEqual([d["verdict"] for d in self.engine.store.decisions()], ["accept", "reopen"])
        self.assertEqual(self.engine.store.ticket(1)["run_gen"], 2)
        flat = json.dumps(self.llm.requests[-1]["messages"])
        self.assertIn("first job", flat, "the reopened ticket lost its session")
        self.assertIn("one more thing", flat)
        self.until(self.settled, what="the outbox")
        texts = [p.get("text", "") for p in self.outbox_payloads()]
        self.assertTrue(any("Ticket 1 reopened" in t for t in texts))

    def test_reopening_changes_neither_the_mandate_nor_the_hook(self):
        """F14. A reopened ticket may do exactly what it could before: the files the harness reads
        its limits from are byte for byte the same."""
        self.ended_ticket()
        store = self.engine.store
        base = os.path.dirname(store.ticket(1)["session"])
        paths = [os.path.join(base, "mandate.json"), os.path.join(store.ticket(1)["cwd"], ".harness.hooks.json")]
        before = [self.read_bytes(p) for p in paths]
        row = store._q("SELECT body FROM mandate WHERE ticket_id=1")[0]["body"]
        self.llm.script[:] = [{"text": "again"}]
        self.drop("002.json", text="more", addressed=False)
        self.until(lambda: self.states() == ["done", "done"], what="the reopened ticket")
        self.assertEqual([self.read_bytes(p) for p in paths], before)
        self.assertEqual(store._q("SELECT body FROM mandate WHERE ticket_id=1")[0]["body"], row)

    def test_a_ticket_a_person_stopped_is_never_reopened(self):
        """F10. A stop is final. A follow-up does not bring it back, and it is not turned into a
        new ticket either (that would route around the stop)."""
        self.remake(**IDLE)
        self.llm.script[:] = [{"text": "ok"}]
        self.drop("001.json", text="start")
        self.until(lambda: self.states() == ["done"], what="the turn")
        self.engine.stop_ticket(1)
        self.drop("002.json", text="come back", addressed=False)
        self.settle()
        self.assertEqual(self.engine.store.ticket(1)["state"], "stopped")
        self.assertEqual(len(self.engine.store.tickets()), 1)
        self.assertNotIn("reopen", [d["verdict"] for d in self.engine.store.decisions()])

    def test_a_stranger_does_not_wake_an_ended_ticket(self):
        """Being on a thread that once had a ticket gives a stranger no standing."""
        self.ended_ticket()
        self.drop("002.json", sender="mallory", text="wake up", addressed=False)
        self.settle()
        self.assertEqual(self.engine.store.ticket(1)["state"], "ended")
        self.assertEqual([d["verdict"] for d in self.engine.store.decisions()][1:], ["ignore"])
        self.assertEqual(self.states(), ["done"])

    def test_a_sender_who_left_the_allow_list_does_not_wake_it(self):
        """F8. Admission is decided now, for this message, by the policy in force now; the old
        ticket's mandate is not a standing invitation."""
        from superagent.gate import Policy
        self.ended_ticket()
        engine2 = self.restart_with(Policy(allowed_senders=frozenset({"bob"}), accept_mode="observe"))
        self.drop("002.json", sender="alice", text="please continue", addressed=False)
        self.settle()
        self.assertEqual(engine2.store.ticket(1)["state"], "ended")
        self.assertEqual([d["verdict"] for d in engine2.store.decisions()][1:], ["ignore"])

    def test_an_explicit_request_still_reopens_when_the_accept_mode_is_off(self):
        """F9. `off` only stops inferred work from starting by itself; a person on the allow list
        asking is still an explicit request."""
        from superagent.gate import Policy
        self.ended_ticket()
        engine2 = self.restart_with(Policy(allowed_senders=frozenset({"alice"}), accept_mode="off"))
        self.llm.script[:] = [{"text": "yes"}]
        self.drop("002.json", sender="alice", text="please continue", addressed=False)
        self.until(lambda: self.states(engine2) == ["done", "done"], engine=engine2, what="the reopened ticket")
        self.assertEqual([d["verdict"] for d in engine2.store.decisions()], ["accept", "reopen"])

    def test_no_follow_up_is_delivered_unless_the_sender_is_admitted(self):
        """The live-ticket case with every kind of sender: a person on the allow list is delivered
        (also when the mode is `off`); a stranger and a bot that is only 'inferred' are not."""
        from superagent.gate import Policy
        cases = [("alice", "observe", "fanout"), ("mallory", "observe", "ignore"),
                 ("ci-bot", "observe", "ignore"), ("alice", "off", "fanout")]
        for sender, mode, expect in cases:
            with self.subTest(sender=sender, mode=mode):
                self.tearDown()
                self.setUp()
                self.remake(policy=Policy(allowed_senders=frozenset({"alice"}), bot_senders=frozenset({"ci-bot"}),
                                          accept_mode=mode))
                self.llm.script[:] = [{"text": "ok"}, {"text": "ok"}]
                self.drop("001.json", text="start")
                self.until(lambda: self.states() == ["done"], what="the first turn")
                self.drop("002.json", sender=sender, text="follow-up", addressed=False)
                self.settle()
                self.assertEqual(self.engine.store.decisions()[-1]["verdict"], expect)

    def test_a_reopened_ticket_waits_for_a_free_slot_and_does_not_push_another_out(self):
        """F7. With one slot, a reopened ticket queues behind the running one."""
        self.remake(max_running=1, **IDLE)
        question = {"calls": [("run_command", {"command": "echo x"})]}
        self.llm.script[:] = [{"text": "a1"}, question, {"text": "b done"}, {"text": "a2"}]
        self.drop("001.json", thread="A", text="job a")
        self.until(lambda: self.engine.store.ticket(1) and self.engine.store.ticket(1)["state"] == "ended",
                   what="ticket A to end")
        self.drop("002.json", thread="B", text="job b")
        self.until(lambda: self.engine.store.open_asks(), what="B to ask a question")
        self.drop("003.json", thread="A", text="job a again", addressed=False)
        self.settle(30)
        self.assertEqual(self.engine.store.ticket(1)["state"], "open")
        self.assertEqual(self.states(tid=1), ["done", "pending"])
        self.assertEqual(list(self.engine.runs), [2], "the running ticket was pushed out")
        self.engine.answer_ask(self.engine.store.open_asks()[0]["ask_id"], False)
        self.until(lambda: self.states(tid=1) == ["done", "done"], timeout=90, what="A to get its slot")
        self.assertEqual(self.states(tid=2), ["done"])


class TestReplies(Lifecycle):
    def test_a_crash_while_finishing_a_turn_neither_loses_nor_doubles_the_reply(self):
        """F1. The orchestrator dies in the middle of the transaction that marks the input done and
        queues the reply. Nothing of it is left; after a restart the input is replayed and its reply
        is queued exactly once."""
        self.llm.script[:] = [{"text": "FIRST-attempt"}, {"text": "SECOND-after-replay"}]
        self.drop("001.json", text="do it")

        def boom(point):
            if point == "in_finish_turn":
                raise te.Crash()
        self.engine.fault = boom
        with self.assertRaises(te.Crash):
            for _ in range(300):
                self.engine.tick()
                time.sleep(0.1)
        for run in list(self.engine.runs.values()):
            run.executor.stop(force=True)
        self.assertEqual(self.engine.store.outbox_rows("pending")[1:], [], "a half-made reply was left behind")
        self.assertEqual([self.engine.store.inputs(1)[0]["state"]], ["sent"])
        self.stores[0].close()                                      # the orchestrator is gone
        engine2 = self.make_engine()
        engine2.recover()
        self.until(lambda: self.states(engine2) == ["done"] and engine2.store.outbox_rows("pending") == [],
                   engine=engine2, what="the replayed turn and its reply")
        replies = [p for p in self.outbox_payloads() if p.get("kind") == "executor_output"]
        self.assertEqual([p["text"] for p in replies], ["SECOND-after-replay"])

    def test_a_turn_with_no_reply_still_tells_the_person(self):
        """F3 / D1. A failed model call ends the turn with no text. Silence would look like 'still
        working'; one short status message per input says it ended, and does not repeat."""
        self.llm.script[:] = [{"status": 503}] * 40
        self.drop("001.json", text="do it")
        self.until(lambda: self.states() == ["done"] and self.settled(), timeout=60, what="the failed turn")
        payloads = [p for p in self.outbox_payloads() if p["msg_id"].startswith("reply-")]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["kind"], "status")
        self.assertIn("without a reply", payloads[0]["text"])
        self.assertNotIn("executor_output", [p.get("kind") for p in self.outbox_payloads()])

    def test_an_event_of_an_older_process_cannot_end_the_current_turn(self):
        """F3c. After a restart, late events of the old process can still be in the queue. Fed into
        the reply tracker they would end the turn early and report the OLD text as the answer."""
        store = self.engine.store
        tid, _ = store.new_ticket("s", "t", Strength.EXPLICIT, self.engine.paths)
        trid = store.add_trigger(Trigger("s", "e", "t", "alice", "hi", True, 1.0))
        iid = store.enqueue_input(tid, trid, "k")
        store.mark_input(iid, InputState.SENT)

        class Stub:                                                 # a process that is not really there
            events = queue.Queue()
        stub = Stub()
        run = _Run(tid, stub, 1, 111)
        # The current process writes its answer; late events of the old one arrive in the MIDDLE of
        # the turn (one that would void the answer, one that would replace it); then the turn ends.
        for ev in ({"kind": "assistant", "text": "NEW", "_pid": 111},
                   {"kind": "tool_result", "_pid": 999},
                   {"kind": "assistant", "text": "OLD", "_pid": 999},
                   {"kind": "turn_end", "_pid": 111}):
            stub.events.put(ev)
        self.engine.pump(run)
        rows = store.outbox_rows()
        replies = [json.loads(r["payload"]) for r in rows if r["msg_id"].startswith("reply-")]
        self.assertEqual([r["text"] for r in replies], ["NEW"])

    def test_the_reply_is_cleaned_and_capped(self):
        """F13. The model's words leave the system as data: no control characters, bounded size, and
        the cut is reported."""
        self.llm.script[:] = [{"text": "start\x1b[31m red\x00" + "z" * 5000}]
        self.drop("001.json", text="x")
        self.until(lambda: self.states() == ["done"] and self.settled(), what="the turn")
        reply = [p for p in self.outbox_payloads() if p.get("kind") == "executor_output"][0]
        self.assertEqual(len(reply["text"]), 4000)
        self.assertTrue(reply["text"].startswith("start[31m red"))
        self.assertNotIn("\x1b", reply["text"])
        self.assertGreater(reply["truncated_chars"], 0)


if __name__ == "__main__":
    unittest.main()
