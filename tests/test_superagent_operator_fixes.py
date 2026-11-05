"""Four things a first-time operator ran into (小o's hands-on round, reproduced by hand).

1. a wrong --base-url looked like a turn that simply had no reply;
2. `stop <no such ticket>` leaked a TypeError;
3. an event without `thread` silently used its `id` as the thread (kept - it is documented now);
4. `down --mode stop` with several tickets stopped them one after the other, so the control side gave up
   before the orchestrator was done.

Each test says why the behaviour matters: a test that still passes after the fix is removed would be useless.
"""
import json
import os
import threading
import unittest

import test_superagent_engine as te
from superagent import lifecycle
from superagent.engine import Engine, _Run
from superagent.model import Strength, TicketState
from superagent.gate import Policy, RuleAssessor
from superagent.sources import FileInbox
from superagent.store import Store, TicketRejected
from superagent.turnreply import ReplyTracker, TurnEnd, reply_payload
import superagent.__main__ as cli


FAILED = "\n[harness] model call failed, turn abandoned: HTTP 404: {\"detail\":\"Not Found\"}"


class TestFailureIsNotSilence(unittest.TestCase):
    """A mistyped endpoint must not look like a model that chose to say nothing."""

    def test_the_tracker_keeps_the_failure_of_this_turn_only(self):
        t = ReplyTracker()
        t.feed({"kind": "turn_start"})
        t.feed({"kind": "log", "text": FAILED})
        self.assertEqual(t.feed({"kind": "turn_end"}).failure, FAILED.strip())
        t.feed({"kind": "turn_start"})
        self.assertIsNone(t.feed({"kind": "turn_end"}).failure,
                          "last turn's failure was reported again for a turn that did not fail")

    def test_a_failure_line_outside_any_turn_is_not_blamed_on_the_next_turn(self):
        t = ReplyTracker()
        t.feed({"kind": "log", "text": FAILED})
        t.feed({"kind": "turn_start"})
        self.assertIsNone(t.feed({"kind": "turn_end"}).failure)

    def test_a_failure_before_a_later_good_answer_is_not_reported(self):
        t = ReplyTracker()
        t.feed({"kind": "turn_start"})
        t.feed({"kind": "log", "text": FAILED})
        t.feed({"kind": "assistant", "text": "done after all"})
        end = t.feed({"kind": "turn_end"})
        self.assertEqual((end.text, end.failure), ("done after all", None))

    def test_the_reason_travels_as_untrusted_detail_not_as_the_system_sentence(self):
        p = reply_payload(3, 17, None, FAILED.strip())
        self.assertEqual((p["kind"], p["ticket_id"], p["input_id"]), ("status", 3, 17))
        self.assertIn("model call failed", p["text"])
        self.assertIn("HTTP 404", p["detail"])
        self.assertTrue(p["untrusted"], "the endpoint's own words are data, like executor_output")
        self.assertNotIn("HTTP 404", p["text"], "the system sentence must stay fixed")

    def test_the_detail_is_cleaned_and_short(self):
        p = reply_payload(1, 1, None, "[harness] model call failed: \x1b[31m" + "x" * 5000)
        self.assertNotIn("\x1b", p["detail"])
        self.assertLessEqual(len(p["detail"]), 300)
        p = reply_payload(1, 1, None, "[harness] model call failed: HTTP 500\nsecond line of the body")
        self.assertNotIn("second line", p["detail"], "one line only: the rest is the endpoint's free text")

    def test_no_failure_keeps_the_old_message_exactly(self):
        p = reply_payload(3, 17, None)
        self.assertEqual(p, {"kind": "status", "ticket_id": 3, "input_id": 17,
                             "text": "Ticket 3 finished a turn without a reply (see its folder)."})

    def test_a_reply_with_text_never_carries_a_detail(self):
        self.assertNotIn("detail", reply_payload(3, 17, "ok", FAILED.strip()))


class TestFailureEndToEnd(te.Rig):
    def out_messages(self):
        out = []
        for n in sorted(os.listdir(self.out_dir)):
            with open(os.path.join(self.out_dir, n), encoding="utf-8") as f:
                out.append(json.load(f))
        return out

    def test_the_outbox_message_names_the_http_error(self):
        """The mistyped-URL case from the hands-on round: the model endpoint answers 404, and the
        operator has to be able to tell that from a model that had nothing to say."""
        self.llm.script[:] = [{"status": 404}]
        self.drop("001.json", text="please write notes.txt")
        self.until(lambda: any("detail" in m for m in self.out_messages()), what="a failure notice")
        msg = next(m for m in self.out_messages() if "detail" in m)
        self.assertEqual(msg["kind"], "status")
        self.assertIn("404", msg["detail"])
        self.assertIn("model call failed", msg["text"])


class TestStopOfAnUnknownTicket(te.Rig):
    def test_the_engine_names_the_ticket_instead_of_leaking_a_type_error(self):
        with self.assertRaises(TicketRejected) as cm:
            self.engine.stop_ticket(999)
        self.assertIn("999", str(cm.exception))
        self.assertEqual(self.engine.store.tickets(), [], "refusing must not create or change anything")

    def test_resume_of_an_unknown_ticket_is_refused_too(self):
        """It used to answer 'resumed' for a ticket that does not exist."""
        with self.assertRaises(TicketRejected):
            self.engine.resume(999)

    def test_the_command_channel_reports_it_as_a_refusal(self):
        class Ch:
            def __init__(self):
                self.got = None

            def reply(self, name, ok, message=""):
                self.got = (ok, message)
        ch = Ch()
        cli.dispatch(self.engine, ch, "n1", {"cmd": "stop", "ticket": 999})
        self.assertEqual(ch.got[0], False)
        self.assertIn("TicketRejected", ch.got[1])
        self.assertNotIn("NoneType", ch.got[1])

    def test_a_real_ticket_still_stops(self):
        tid, _ = self.engine.store.new_ticket("s", "t", Strength.EXPLICIT, lambda i: ("c", "s", "c"))
        self.engine.stop_ticket(tid)
        self.assertEqual(self.engine.store.ticket(tid)["state"], TicketState.STOPPED.value)


class TestEventDefaults(unittest.TestCase):
    """The file inbox fills gaps quietly. That is a choice, and these tests are what the docs point at."""

    def read(self, raw):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            inbox = FileInbox(d)
            p = os.path.join(d, "e.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(raw, f)
            old = os.path.getmtime(p) - 10
            os.utime(p, (old, old))
            return inbox.poll()

    def test_a_missing_thread_becomes_the_id_so_each_such_message_is_its_own_conversation(self):
        (t,) = self.read({"id": "m7", "sender": "alice", "text": "hi", "addressed": True})
        self.assertEqual(t.thread, "m7")

    def test_a_missing_sender_is_unknown_and_so_is_never_on_an_allow_list(self):
        (t,) = self.read({"id": "m8", "thread": "T", "text": "hi", "addressed": True})
        self.assertEqual(t.sender, "unknown")

    def test_a_missing_addressed_means_not_addressed(self):
        (t,) = self.read({"id": "m9", "thread": "T", "sender": "alice", "text": "hi"})
        self.assertFalse(t.addressed)

    def test_a_missing_id_falls_back_to_the_file_name(self):
        (t,) = self.read({"thread": "T", "sender": "alice", "text": "hi"})
        self.assertEqual(t.ext_id, "e.json")


class _SlowStop:
    """Stands in for an executor: stop() only returns once every other stop() is running too."""

    def __init__(self, barrier):
        self.barrier, self.stopped, self.alive = barrier, False, True
        self.events = None

    def stop(self, force=False, grace=5.0):
        self.barrier.wait()                     # BrokenBarrierError after the timeout if stops run one by one
        self.stopped = True


class TestShutdownStopsTogether(unittest.TestCase):
    def test_the_graceful_waits_overlap_instead_of_adding_up(self):
        """A ticket that waits for an approval never answers /quit, so each stop burns its whole
        grace period. One after the other, 7 tickets take 7 grace periods; the control side's
        30 seconds ran out first and `down` reported a failure for a shutdown that then succeeded."""
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            store = Store(home).open()
            try:
                engine = Engine(store, None, None, Policy(), RuleAssessor(), lambda t: None)
                barrier = threading.Barrier(4, timeout=5)
                stubs = []
                for _ in range(4):
                    tid, _n = store.new_ticket("s", "t%d" % len(stubs), Strength.EXPLICIT,
                                               lambda i: ("c", "s", "c"))
                    pid = 100 + len(stubs)
                    gen = store.start_run(tid, pid, 1.0, "c")
                    stub = _SlowStop(barrier)
                    stubs.append(stub)
                    engine.runs[tid] = _Run(tid, stub, gen, pid)
                self.assertEqual(len(store._q("SELECT * FROM process")), 4)
                engine.shutdown()
                self.assertTrue(all(s.stopped for s in stubs))
                self.assertEqual(engine.runs, {})
                self.assertEqual(store._q("SELECT * FROM process"), [],
                                 "the process list must be emptied, or a restart would try to kill pids it does not own")
            finally:
                store.close()


class _BrokenStop:
    alive = True
    events = None

    def stop(self, force=False, grace=5.0):
        raise OSError("taskkill could not be started")


class TestShutdownDoesNotForgetAProcessItFailedToStop(unittest.TestCase):
    def test_a_failed_stop_keeps_its_process_record_and_is_still_raised(self):
        """The process record is the only thing that lets the next start or `down` find a child that is
        still alive. Dropping it because stop() raised would leave an orphan nobody knows about."""
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            store = Store(home).open()
            try:
                engine = Engine(store, None, None, Policy(), RuleAssessor(), lambda t: None)
                good = _SlowStop(threading.Barrier(1, timeout=5))
                for pid, stub in ((200, _BrokenStop()), (201, good)):
                    tid, _n = store.new_ticket("s", "t%d" % pid, Strength.EXPLICIT, lambda i: ("c", "s", "c"))
                    gen = store.start_run(tid, pid, 1.0, "c")
                    engine.runs[tid] = _Run(tid, stub, gen, pid)
                with self.assertRaises(OSError):
                    engine.shutdown()
                self.assertTrue(good.stopped, "one executor's failure must not leave the others running")
                self.assertEqual([r["pid"] for r in store._q("SELECT pid FROM process")], [200])
                self.assertEqual(engine.runs, {})
            finally:
                store.close()


class TestDownSaysWhatIsHappening(unittest.TestCase):
    def test_a_slow_orchestrator_is_not_reported_as_one_that_ignored_the_request(self):
        """The operator reads this message to decide whether to retry. 'Did not answer' made them
        think the shutdown had failed, while the orchestrator was still closing its tickets."""
        import tempfile
        from unittest import mock

        class SilentChannel:
            def __init__(self, home):
                pass

            def submit(self, cmd, **kw):
                return "n1"

            def wait(self, name, timeout=30.0):
                return None

        with tempfile.TemporaryDirectory() as home,                 mock.patch.object(lifecycle, "running", return_value=True),                 mock.patch("superagent.control.Channel", SilentChannel):
            with self.assertRaises(RuntimeError) as cm:
                lifecycle.down(home, "stop", wait_for_engine=0.1)
        msg = str(cm.exception)
        self.assertIn("shutting down", msg)
        self.assertIn("status", msg, "say how to find out whether it finished")


if __name__ == "__main__":
    unittest.main()
