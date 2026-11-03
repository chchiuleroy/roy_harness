"""Store tests: idempotence, the immutable mandate, the delivery ledger, question identity and
the home lock. Each test's docstring says WHY the behaviour matters."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import superagent_support as sup
from superagent.model import AskState, Decision, InputState, Strength, TicketState, Trigger, Verdict
from superagent.store import AskRejected, Store, StoreLocked


def trig(ext="e1", thread="t1", text="hi"):
    return Trigger("file:inbox", ext, thread, "alice", text, True, 1.0)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.s = Store(self.home).open()

    def tearDown(self):
        self.s.close()
        self._tmp.cleanup()

    def ticket(self, thread="t1"):
        tid, m = self.s.new_ticket("file:inbox", thread, Strength.EXPLICIT,
                                   lambda i: (os.path.join(self.home, f"w{i}"), os.path.join(self.home, f"s{i}.json"),
                                              os.path.join(self.home, f"w{i}")))
        return tid, m


class TestIdempotence(StoreCase):
    def test_same_trigger_twice_is_stored_once(self):
        """A restart or a copied inbox file replays events; the second copy must not become a
        second ticket."""
        self.assertIsNotNone(self.s.add_trigger(trig()))
        self.assertIsNone(self.s.add_trigger(trig()))
        self.assertEqual(self.s._q("SELECT COUNT(*) AS n FROM trigger")[0]["n"], 1)

    def test_delivery_key_is_unique(self):
        tid, _ = self.ticket()
        trid = self.s.add_trigger(trig())
        self.assertIsNotNone(self.s.enqueue_input(tid, trid, "k1"))
        self.assertIsNone(self.s.enqueue_input(tid, trid, "k1"))

    def test_transaction_rolls_back_on_error(self):
        """A half-written ticket (row without mandate) would run with no limits."""
        with self.assertRaises(RuntimeError):
            self.s.new_ticket("file:inbox", "t1", Strength.EXPLICIT,
                              lambda i: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(self.s.tickets(), [])


class TestMandate(StoreCase):
    def test_mandate_cannot_be_changed_or_deleted(self):
        """The mandate says why the work may be done and how far; widening it must be a new
        ticket, so the database refuses UPDATE and DELETE itself."""
        tid, _ = self.ticket()
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute("UPDATE mandate SET body='{}' WHERE ticket_id=?", (tid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute("DELETE FROM mandate WHERE ticket_id=?", (tid,))
        self.assertEqual(self.s.mandate(tid)["ticket_id"], tid)

    def test_stop_is_final(self):
        """A person's stop must not be undone by an event that arrives later."""
        tid, _ = self.ticket()
        self.s.set_ticket_state(tid, TicketState.STOPPED)
        self.assertFalse(self.s.set_ticket_state(tid, TicketState.OPEN))
        self.assertEqual(self.s.ticket(tid)["state"], "stopped")
        self.assertEqual(self.s.live_tickets_on("file:inbox", "t1"), [])


class TestLedger(StoreCase):
    def feed(self, n=3):
        tid, _ = self.ticket()
        ids = []
        for i in range(n):
            trid = self.s.add_trigger(trig(f"e{i}", text=f"m{i}"))
            ids.append(self.s.enqueue_input(tid, trid, f"k{i}"))
        return tid, ids

    def test_one_input_in_flight_per_ticket(self):
        """With one outstanding input, the next turn_end can only belong to it."""
        tid, ids = self.feed()
        first = self.s.next_deliverable(tid)
        self.assertEqual(first["id"], ids[0])
        self.s.mark_input(first["id"], InputState.SENT)
        self.assertIsNone(self.s.next_deliverable(tid))
        self.s.mark_input(first["id"], InputState.DONE)
        self.assertEqual(self.s.next_deliverable(tid)["id"], ids[1])

    def test_recover_turns_sent_into_replay_and_replays_first(self):
        """After a crash the in-flight input may or may not have been handled: it goes first
        and is marked, so the model is told to check before acting."""
        tid, ids = self.feed()
        self.s.mark_input(ids[0], InputState.SENT)
        self.assertEqual(self.s.recover_inflight(), [ids[0]])
        nxt = self.s.next_deliverable(tid)
        self.assertEqual((nxt["id"], nxt["state"], nxt["replayed"]), (ids[0], "replay", 1))

    def test_done_inputs_are_never_replayed(self):
        tid, ids = self.feed(1)
        self.s.mark_input(ids[0], InputState.DONE)
        self.assertEqual(self.s.recover_inflight(), [])
        self.assertIsNone(self.s.next_deliverable(tid))


class TestAskIdentity(StoreCase):
    def test_ask_ids_never_repeat_across_runs(self):
        """The harness restarts its own question counter at q1 on every start; the id a person
        answers must not."""
        tid, _ = self.ticket()
        g1 = self.s.start_run(tid, 100, 1.0, "w")
        a1 = self.s.add_ask(tid, g1, "q1", "run_command: dir")
        self.s.end_run(tid, 100)
        g2 = self.s.start_run(tid, 101, 2.0, "w")
        a2 = self.s.add_ask(tid, g2, "q1", "run_command: del *")
        self.assertNotEqual(a1, a2)
        self.assertEqual((g1, g2), (1, 2))

    def test_late_answer_after_restart_is_refused(self):
        """A person still holding the old question's id must not approve the new one."""
        tid, _ = self.ticket()
        g1 = self.s.start_run(tid, 100, 1.0, "w")
        old = self.s.add_ask(tid, g1, "q1", "dir")
        self.s.end_run(tid, 100)
        g2 = self.s.start_run(tid, 101, 2.0, "w")
        new = self.s.add_ask(tid, g2, "q1", "del *")
        with self.assertRaises(AskRejected):
            self.s.claim_answer(old)
        self.assertEqual(self.s.ask(new)["state"], AskState.OPEN.value)
        self.assertEqual(self.s.claim_answer(new)["rid"], "q1")

    def test_question_of_a_dead_process_is_orphaned_at_once(self):
        """Nobody is waiting for the answer any more. It must be refused immediately, not only
        once some later run happens to change the generation."""
        tid, _ = self.ticket()
        g = self.s.start_run(tid, 100, 1.0, "w")
        a = self.s.add_ask(tid, g, "q1", "dir")
        self.s.end_run(tid, 100)
        self.assertEqual(self.s.ask(a)["state"], AskState.ORPHANED.value)
        self.assertEqual(self.s.open_asks(tid), [])
        with self.assertRaises(AskRejected):
            self.s.claim_answer(a)

    def test_an_answer_the_harness_never_showed_it_received_is_not_reported_as_given(self):
        """Writing to the pipe succeeds even if the child dies before reading it. When the process
        ends with no sign of progress, the answer is marked orphaned, so the status is truthful."""
        tid, _ = self.ticket()
        g = self.s.start_run(tid, 100, 1.0, "w")
        a = self.s.add_ask(tid, g, "q1", "dir")
        self.s.mark_answered(a)
        self.assertEqual(self.s.end_run(tid, 100), [a])
        self.assertEqual(self.s.ask(a)["state"], AskState.ORPHANED.value)

    def test_an_answer_followed_by_progress_stays_answered(self):
        tid, _ = self.ticket()
        g = self.s.start_run(tid, 100, 1.0, "w")
        a = self.s.add_ask(tid, g, "q1", "dir")
        self.s.mark_answered(a)
        self.s.confirm_answers(tid)
        self.assertEqual(self.s.end_run(tid, 100), [])
        self.assertEqual(self.s.ask(a)["state"], AskState.ANSWERED.value)

    def test_an_answer_is_accepted_once(self):
        tid, _ = self.ticket()
        g = self.s.start_run(tid, 100, 1.0, "w")
        a = self.s.add_ask(tid, g, "q1", "dir")
        self.s.claim_answer(a)
        with self.assertRaises(AskRejected):
            self.s.claim_answer(a)

    def test_run_generation_mismatch_is_refused_even_if_still_open(self):
        """Defence in depth: even if an old question were somehow still open, its run is not
        the current one."""
        tid, _ = self.ticket()
        g = self.s.start_run(tid, 100, 1.0, "w")
        a = self.s.add_ask(tid, g, "q1", "dir")
        self.s.start_run(tid, 101, 2.0, "w")            # new run without end_run of the old one
        with self.assertRaises(AskRejected):
            self.s.claim_answer(a)


class TestNestedTransactions(StoreCase):
    def test_inner_transactions_join_the_outer_one(self):
        """The engine records a trigger, the decision, the ticket and its first input as ONE
        commit. If anything in the middle fails, none of it may stay."""
        class Boom(Exception):
            pass
        with self.assertRaises(Boom):
            with self.s.tx():
                trid = self.s.add_trigger(trig())
                tid, _ = self.ticket()
                self.s.enqueue_input(tid, trid, "k")
                raise Boom()
        self.assertEqual(self.s.tickets(), [])
        self.assertEqual(self.s._q("SELECT COUNT(*) AS n FROM trigger")[0]["n"], 0)
        self.assertEqual(self.s._q("SELECT COUNT(*) AS n FROM input")[0]["n"], 0)

    def test_everything_inside_is_kept_when_nothing_fails(self):
        with self.s.tx():
            trid = self.s.add_trigger(trig())
            tid, _ = self.ticket()
            self.s.enqueue_input(tid, trid, "k")
        self.assertEqual(len(self.s.tickets()), 1)
        self.assertEqual(len(self.s.inputs(tid)), 1)

    def test_an_inner_failure_that_the_caller_swallows_still_rolls_everything_back(self):
        """Catching the error does not make half-done work valid: the outer transaction must not
        commit what the failed inner one wrote."""
        with self.assertRaises(RuntimeError) as ctx:
            with self.s.tx():
                self.s.add_trigger(trig("kept?"))
                try:
                    with self.s.tx():
                        self.s.add_trigger(trig("inner"))
                        raise ValueError("inner step failed")
                except ValueError:
                    pass                          # swallowed
        self.assertIn("rolled back", str(ctx.exception))
        self.assertEqual(self.s._q("SELECT COUNT(*) AS n FROM trigger")[0]["n"], 0)
        self.assertIsNotNone(self.s.add_trigger(trig("after")), "the store is unusable after the rollback")

    def test_a_failed_transaction_leaves_the_store_usable(self):
        try:
            with self.s.tx():
                self.s.add_trigger(trig("a"))
                raise ValueError()
        except ValueError:
            pass
        self.assertIsNotNone(self.s.add_trigger(trig("a")))


class TestAnswerable(StoreCase):
    def test_checking_does_not_consume_the_question(self):
        """The engine must be able to look, fail to deliver, and look again."""
        tid, _ = self.ticket()
        g = self.s.start_run(tid, 100, 1.0, "w")
        a = self.s.add_ask(tid, g, "q1", "dir")
        self.assertEqual(self.s.answerable(a)["ask_id"], a)
        self.assertEqual(self.s.answerable(a)["ask_id"], a)
        self.assertEqual(self.s.ask(a)["state"], AskState.OPEN.value)
        self.s.mark_answered(a)
        with self.assertRaises(AskRejected):
            self.s.answerable(a)


class TestOlderDatabase(unittest.TestCase):
    def make_old_home(self, d):
        """A home as the first version created it: the ask table has no `confirmed` column."""
        import sqlite3
        from superagent import store as store_mod
        old = store_mod.SCHEMA.replace("ts REAL NOT NULL,\n  confirmed INTEGER NOT NULL DEFAULT 0);",
                                       "ts REAL NOT NULL);")
        self.assertNotEqual(old, store_mod.SCHEMA, "the test no longer builds the old schema")
        db = sqlite3.connect(os.path.join(d, "state.sqlite"))
        db.executescript(old)
        db.execute("INSERT INTO ticket(id, source, thread, state, created) VALUES(1,'s','t','open',1.0)")
        db.execute("INSERT INTO ask(ticket_id, run_gen, rid, what, what_sha, state, ts) "
                   "VALUES(1,1,'q1','dir','x','answered',1.0)")
        db.commit()
        db.close()

    def test_a_home_made_by_the_first_version_is_upgraded_not_broken(self):
        """Found by review: the new `confirmed` column was only in CREATE TABLE IF NOT EXISTS, so an
        existing home failed with 'no such column' the first time an answer was confirmed."""
        with tempfile.TemporaryDirectory() as d:
            self.make_old_home(d)
            with Store(d) as s:
                cols = {r["name"] for r in s._q("PRAGMA table_info(ask)")}
                self.assertIn("confirmed", cols)
                self.assertEqual(s.ask(1)["confirmed"], 1, "old history must not look like an unseen answer")
                s.confirm_answers(1)                          # the statement that used to fail
                self.assertEqual(s.end_run(1, 99), [])
                g = s.start_run(1, 100, 1.0, "w")
                a = s.add_ask(1, g, "q1", "dir")
                self.assertEqual(s.ask(a)["confirmed"], 0)
            with Store(d) as s:                               # and a second open changes nothing
                self.assertEqual(s.ask(1)["confirmed"], 1)

    def test_a_new_home_needs_no_upgrade(self):
        with tempfile.TemporaryDirectory() as d:
            with Store(d) as s:
                g = None
                tid, _ = s.new_ticket("s", "t", Strength.EXPLICIT, lambda i: ("c", "s", "c"))
                g = s.start_run(tid, 1, 1.0, "c")
                a = s.add_ask(tid, g, "q1", "dir")
                self.assertEqual(s.ask(a)["confirmed"], 0)


class TestOpenFailure(unittest.TestCase):
    def test_a_failed_open_gives_the_lock_back(self):
        """Found by review: if the upgrade step raised, open() kept the home lock and a
        half-open connection, so a caller that caught the error and tried again was locked out by
        itself."""
        with tempfile.TemporaryDirectory() as d:
            broken = Store(d)

            def boom():
                raise RuntimeError("upgrade failed")
            broken._migrate = boom
            with self.assertRaises(RuntimeError):
                broken.open()
            self.assertIsNone(broken.db)
            with Store(d, lock_wait=0) as again:          # no waiting: the lock must already be free
                self.assertEqual(again.tickets(), [])


class TestSingleWriter(StoreCase):
    def test_other_threads_cannot_use_the_store(self):
        """SQLite writes are only serialised if one thread owns them; a reader thread that
        'just updates a row' would race the engine."""
        err = []

        def go():
            try:
                self.s.add_trigger(trig("x"))
            except RuntimeError as e:
                err.append(e)
        t = threading.Thread(target=go)
        t.start()
        t.join()
        self.assertEqual(len(err), 1)


class TestHomeLock(unittest.TestCase):
    def test_a_brief_probe_does_not_make_a_starting_orchestrator_fail(self):
        """`status` and `down` look at the lock for an instant. An orchestrator that starts in
        that instant waits a moment and gets it; found by the command-line test, where a
        polling `status` made `run` exit with "in use"."""
        from superagent.store import lock_is_held
        with tempfile.TemporaryDirectory() as d:
            probe = Store(d, lock_wait=0)
            probe._take_lock()
            threading.Timer(0.3, probe._release_lock).start()
            Store(d, lock_wait=3.0).open().close()
            self.assertFalse(lock_is_held(d))

    def test_two_probes_at_once_do_not_mistake_each_other_for_an_orchestrator(self):
        """A probe holds the lock for an instant. A second probe in that instant used to see
        'locked' and report an orchestrator that is not there; `down` would then wait for a
        shutdown nobody can answer."""
        from superagent.store import lock_is_held
        with tempfile.TemporaryDirectory() as d:
            slow = {}
            th = threading.Thread(target=lambda: slow.setdefault("r", lock_is_held(d, _hold=0.6)))
            th.start()
            time.sleep(0.15)                      # the first probe is holding the lock now
            self.assertFalse(lock_is_held(d), "a probe was taken for an orchestrator")
            th.join()
            self.assertFalse(slow["r"])

    def test_a_probe_still_sees_a_real_orchestrator(self):
        from superagent.store import lock_is_held
        with tempfile.TemporaryDirectory() as d:
            with Store(d):
                self.assertTrue(lock_is_held(d))

    def test_looking_at_a_held_lock_is_quick(self):
        """`status` asks 'is it running?' while the orchestrator holds the lock; the answer must
        not take the full waiting time of an orchestrator that is starting up."""
        from superagent.store import lock_is_held
        with tempfile.TemporaryDirectory() as d:
            with Store(d):
                t0 = time.time()
                self.assertTrue(lock_is_held(d))
                self.assertLess(time.time() - t0, 0.5)

    def test_a_real_second_orchestrator_still_fails_after_the_wait(self):
        with tempfile.TemporaryDirectory() as d:
            with Store(d):
                t0 = time.time()
                with self.assertRaises(StoreLocked):
                    Store(d, lock_wait=0.4).open()
                self.assertGreaterEqual(time.time() - t0, 0.35)

    def test_second_open_fails_while_first_is_open(self):
        """Two orchestrators on one home would both write the ledger."""
        with tempfile.TemporaryDirectory() as d:
            with Store(d):
                with self.assertRaises(StoreLocked):
                    Store(d).open()
            Store(d).open().close()          # released on close

    def test_lock_is_released_when_the_holder_is_killed(self):
        """A lock file that survives a crash would block every restart forever."""
        with tempfile.TemporaryDirectory() as d:
            code = ("import sys, time; sys.path.insert(0, %r); from superagent.store import Store; "
                    "s = Store(%r).open(); print('held', flush=True); time.sleep(60)" % (sup.SRC, d))
            p = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code], stdout=subprocess.PIPE,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                self.assertEqual(p.stdout.readline().strip(), b"held")
                with self.assertRaises(StoreLocked):
                    Store(d).open()
            finally:
                p.kill()
                p.wait()
                p.stdout.close()
            deadline = time.time() + 10
            while True:
                try:
                    Store(d).open().close()
                    break
                except StoreLocked:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
