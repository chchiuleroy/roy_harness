"""Slice 3, S2: the ledger side of a finished turn: the reply is queued in the same transaction that
marks the input done; activity is recorded so that idle time survives a restart; an older database
is upgraded.

Each test's docstring says WHY the behaviour matters.
"""
import json
import os
import sqlite3
import tempfile
import unittest

import superagent_support as sup  # noqa: F401
from superagent import store as store_mod
from superagent.model import InputState, Strength, TicketState
from superagent.store import Store
from test_superagent_store import StoreCase, trig


class Crash(Exception):
    pass


PAYLOAD = {"kind": "executor_output", "untrusted": True, "ticket_id": 1, "input_id": 1, "text": "done",
           "truncated_chars": 0}


class TurnCase(StoreCase):
    def sent_input(self, ext="e1", thread="t1"):
        """A ticket with one input that has been written to its process (state SENT)."""
        tid, _ = self.ticket(thread)
        trid = self.s.add_trigger(trig(ext, thread))
        iid = self.s.enqueue_input(tid, trid, f"{tid}:{trid}", now=100.0)
        self.s.mark_input(iid, InputState.SENT)
        return tid, iid

    def outbox(self):
        return self.s.outbox_rows()

    def activity(self, tid):
        return self.s.ticket(tid)["last_activity"]


class TestFinishTurn(TurnCase):
    def test_the_input_is_done_and_the_reply_is_queued_together(self):
        """The reply must not be losable: an input marked done with no reply row would never be
        replayed, so the answer would be gone for good."""
        tid, iid = self.sent_input()
        self.assertTrue(self.s.finish_turn(tid, iid, PAYLOAD, now=200.0))
        self.assertEqual(self.s.inputs(tid)[0]["state"], "done")
        rows = self.outbox()
        self.assertEqual([r["msg_id"] for r in rows], [f"reply-{tid}-{iid}"])
        self.assertEqual(json.loads(rows[0]["payload"]), PAYLOAD)
        self.assertEqual((rows[0]["status"], self.activity(tid)), ("pending", 200.0))

    def test_a_crash_before_the_commit_changes_nothing(self):
        """The two writes are one transaction. If it is cut in the middle, the input is still SENT
        (a restart replays it) and there is no reply row to confuse the replay."""
        tid, iid = self.sent_input()

        def boom():
            raise Crash()
        with self.assertRaises(Crash):
            self.s.finish_turn(tid, iid, PAYLOAD, now=200.0, fault=boom)
        self.assertEqual(self.s.inputs(tid)[0]["state"], "sent")
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.activity(tid), 100.0)
        self.assertTrue(self.s.finish_turn(tid, iid, PAYLOAD, now=300.0), "the turn could not be finished again")

    def test_only_a_sent_input_of_that_ticket_can_be_finished(self):
        """A late or repeated turn_end must not queue a reply for an input that is not the one in
        flight, nor for another ticket's input. Nothing at all changes when it is refused."""
        tid, iid = self.sent_input("e1", "t1")
        other, other_iid = self.sent_input("e2", "t2")
        pending_tid, _ = self.ticket("t3")
        pending_trid = self.s.add_trigger(trig("e3", "t3"))
        pending_iid = self.s.enqueue_input(pending_tid, pending_trid, "k3", now=100.0)      # PENDING, never sent
        for t, i in ((tid, other_iid), (tid, 9999), (pending_tid, pending_iid)):
            with self.subTest(ticket=t, input=i):
                self.assertFalse(self.s.finish_turn(t, i, PAYLOAD, now=500.0))
        self.assertEqual(self.outbox(), [])
        self.assertEqual([self.activity(x) for x in (tid, other, pending_tid)], [100.0, 100.0, 100.0])
        self.assertEqual([r["state"] for r in self.s.inputs(tid)], ["sent"])

    def test_finishing_twice_queues_one_reply(self):
        """The reply row's id comes from the turn, and the second call finds the input already done."""
        tid, iid = self.sent_input()
        self.assertTrue(self.s.finish_turn(tid, iid, PAYLOAD, now=200.0))
        self.assertFalse(self.s.finish_turn(tid, iid, PAYLOAD, now=300.0))
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.activity(tid), 200.0)

    def test_a_reply_row_that_already_exists_is_not_duplicated(self):
        """msg_id is unique: even if a row for this turn is somehow there already (a replayed turn
        after a half-recovered crash), the table cannot hold two replies for one input."""
        tid, iid = self.sent_input()
        self.s.outbox_enqueue(tid, {"kind": "status", "text": "x"})
        self.s.db.execute("UPDATE outbox SET msg_id=?", (f"reply-{tid}-{iid}",))
        self.assertTrue(self.s.finish_turn(tid, iid, PAYLOAD, now=200.0))
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.s.inputs(tid)[0]["state"], "done")


class TestActivity(TurnCase):
    def test_a_new_ticket_starts_active(self):
        """Idle time is counted from the last activity; a ticket with no value would be 'idle since
        for ever' and end at once."""
        tid, _ = self.s.new_ticket("s", "t", Strength.EXPLICIT, lambda i: ("c", "s", "c"), now=42.0)
        self.assertEqual((self.activity(tid), self.s.ticket(tid)["created"]), (42.0, 42.0))

    def test_new_work_counts_as_activity_but_a_duplicate_does_not(self):
        tid, _ = self.ticket()
        trid = self.s.add_trigger(trig())
        self.assertIsNotNone(self.s.enqueue_input(tid, trid, "k", now=500.0))
        self.assertEqual(self.activity(tid), 500.0)
        self.assertIsNone(self.s.enqueue_input(tid, trid, "k", now=900.0))
        self.assertEqual(self.activity(tid), 500.0, "a repeated delivery key is not new work")

    def test_a_person_answering_a_question_counts_as_activity(self):
        tid, _ = self.ticket()
        gen = self.s.start_run(tid, 1, 1.0, "c")
        ask = self.s.add_ask(tid, gen, "q1", "run it")
        self.s.mark_answered(ask, now=700.0)
        self.assertEqual(self.activity(tid), 700.0)

    def test_touch_ticket(self):
        tid, _ = self.ticket()
        self.s.touch_ticket(tid, 123.0)
        self.assertEqual(self.activity(tid), 123.0)


class TestEndIfIdle(TurnCase):
    """OPEN -> ENDED only when there is truly nothing left to do. Each veto has its own case, so
    that dropping one of them makes exactly that case fail."""

    def idle_ticket(self, thread="t1"):
        tid, _ = self.ticket(thread)
        self.s.touch_ticket(tid, 100.0)
        return tid

    def end(self, tid, now=200.0, idle_end=50.0):
        return self.s.end_ticket_if_idle(tid, now, idle_end)

    def state(self, tid):
        return self.s.ticket(tid)["state"]

    def test_an_idle_ticket_with_nothing_to_do_ends(self):
        tid = self.idle_ticket()
        self.assertTrue(self.end(tid))
        self.assertEqual(self.state(tid), "ended")
        self.assertFalse(self.end(tid), "an ended ticket ends only once")

    def test_it_must_have_been_idle_long_enough_and_the_boundary_counts(self):
        tid = self.idle_ticket()
        self.assertFalse(self.end(tid, now=149.9))
        self.assertTrue(self.end(tid, now=150.0))

    def test_idle_time_runs_from_the_last_activity_not_from_creation(self):
        tid = self.idle_ticket()
        self.s.touch_ticket(tid, 190.0)
        self.assertFalse(self.end(tid, now=200.0))

    def test_a_ticket_without_a_recorded_activity_counts_from_its_creation(self):
        tid = self.idle_ticket()
        self.s.db.execute("UPDATE ticket SET last_activity=NULL, created=10.0 WHERE id=?", (tid,))
        self.assertTrue(self.end(tid))

    def test_a_note_means_a_person_has_to_look_so_it_does_not_end(self):
        tid = self.idle_ticket()
        self.s.set_ticket_note(tid, "cannot start")
        self.assertFalse(self.end(tid))

    def test_only_an_open_ticket_ends(self):
        for state in ("waiting", "stopped", "ended"):
            with self.subTest(state=state):
                tid = self.idle_ticket(f"t-{state}")
                self.s.db.execute("UPDATE ticket SET state=? WHERE id=?", (state, tid))
                self.assertFalse(self.end(tid))
                self.assertEqual(self.state(tid), state)

    def test_input_that_is_not_finished_keeps_the_ticket_alive(self):
        """pending (not yet sent), sent (a turn is running) and replay (to be sent again) are all
        work that is not done; a finished input is not."""
        for st in (InputState.PENDING, InputState.SENT, InputState.REPLAY, InputState.DONE):
            with self.subTest(input=st.value):
                tid = self.idle_ticket(f"t-{st.value}")
                trid = self.s.add_trigger(trig(f"e-{st.value}", f"t-{st.value}"))
                iid = self.s.enqueue_input(tid, trid, f"k-{st.value}", now=100.0)
                self.s.mark_input(iid, st)
                self.s.touch_ticket(tid, 100.0)
                self.assertEqual(self.end(tid), st is InputState.DONE)

    def test_an_open_question_keeps_the_ticket_alive_and_an_answered_one_does_not(self):
        tid = self.idle_ticket()
        gen = self.s.start_run(tid, 1, 1.0, "c")
        ask = self.s.add_ask(tid, gen, "q1", "run it")
        self.s.touch_ticket(tid, 100.0)
        self.assertFalse(self.end(tid))
        self.s.mark_answered(ask, now=100.0)
        self.assertTrue(self.end(tid))

    def test_an_ended_ticket_is_no_longer_live_for_follow_ups(self):
        tid = self.idle_ticket()
        self.assertEqual(self.s.live_tickets_on("file:inbox", "t1"), [tid])
        self.end(tid)
        self.assertEqual(self.s.live_tickets_on("file:inbox", "t1"), [])


class TestReopen(TurnCase):
    def ended(self, thread="t1", at=100.0):
        tid, _ = self.ticket(thread)
        self.s.touch_ticket(tid, at)
        self.assertTrue(self.s.end_ticket_if_idle(tid, at + 1000, 10.0))
        return tid

    def test_ended_tickets_come_most_recent_first_and_only_ended_ones(self):
        a, b, c = self.ended(at=100.0), self.ended(at=300.0), self.ended(at=200.0)
        live, stopped = self.ticket()[0], self.ticket()[0]
        self.s.set_ticket_state(stopped, TicketState.STOPPED)
        other = self.ended(thread="t2")
        self.assertEqual(self.s.ended_tickets_on("file:inbox", "t1"), [b, c, a])
        self.assertEqual(self.s.ended_tickets_on("file:inbox", "t2"), [other])
        self.assertEqual(self.s.ended_tickets_on("file:inbox", "nothing"), [])
        self.assertNotIn(live, self.s.ended_tickets_on("file:inbox", "t1"))
        self.assertNotIn(stopped, self.s.ended_tickets_on("file:inbox", "t1"))

    def test_reopening_opens_the_ticket_and_queues_the_message_together(self):
        tid = self.ended()
        trid = self.s.add_trigger(trig("e2"))
        iid = self.s.reopen_ticket(tid, trid, f"{tid}:{trid}", now=5000.0)
        self.assertIsNotNone(iid)
        self.assertEqual(self.s.ticket(tid)["state"], "open")
        self.assertEqual([i["state"] for i in self.s.inputs(tid)], ["pending"])
        self.assertEqual(self.s.ticket(tid)["last_activity"], 5000.0)

    def test_only_an_ended_ticket_can_be_reopened_and_nothing_changes_otherwise(self):
        for state in (TicketState.OPEN, TicketState.WAITING, TicketState.STOPPED):
            with self.subTest(state=state.value):
                tid, _ = self.ticket(f"t-{state.value}")
                self.s.set_ticket_state(tid, state)
                trid = self.s.add_trigger(trig(f"e-{state.value}", f"t-{state.value}"))
                self.assertIsNone(self.s.reopen_ticket(tid, trid, f"{tid}:{trid}"))
                self.assertEqual(self.s.ticket(tid)["state"], state.value)
                self.assertEqual(self.s.inputs(tid), [])

    def test_the_mandate_is_not_touched(self):
        tid = self.ended()
        before = self.s.mandate(tid)
        trid = self.s.add_trigger(trig("e2"))
        self.s.reopen_ticket(tid, trid, f"{tid}:{trid}")
        self.assertEqual(self.s.mandate(tid), before)


class TestOlderDatabase(unittest.TestCase):
    def test_a_home_without_last_activity_is_upgraded_and_history_is_not_recounted(self):
        """The new column is only in CREATE TABLE IF NOT EXISTS, which never adds a column to a table
        that exists (the same mistake that broke `ask.confirmed` once). Old tickets count as active
        when they were made, so an upgrade does not make every old ticket look brand new."""
        old = store_mod.SCHEMA.replace("note TEXT, last_activity REAL);", "note TEXT);")
        self.assertNotEqual(old, store_mod.SCHEMA, "the test no longer builds the old schema")
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(os.path.join(d, "state.sqlite"))
            db.executescript(old)
            db.execute("INSERT INTO ticket(id, source, thread, state, created) VALUES(1,'s','t','open',77.0)")
            db.commit()
            db.close()
            with Store(d) as s:
                self.assertIn("last_activity", {r["name"] for r in s._q("PRAGMA table_info(ticket)")})
                self.assertEqual(s.ticket(1)["last_activity"], 77.0)
                s.touch_ticket(1, 99.0)
            with Store(d) as s:                                 # a second open changes nothing
                self.assertEqual(s.ticket(1)["last_activity"], 99.0)


if __name__ == "__main__":
    unittest.main()
