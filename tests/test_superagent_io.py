"""Checkpoint D': the file inbox and the crash-safe outbox.

Each test's docstring says WHY the behaviour matters.
"""
import json
import os
import tempfile
import time
import unittest

import superagent_support as sup  # noqa: F401
from superagent.model import Strength
from superagent.outbox import MAX_ATTEMPTS, Outbox
from superagent.sources import FileInbox, FileSink
from superagent.store import Store


def drop(folder, name, body, age=10.0):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body if isinstance(body, str) else json.dumps(body))
    old = time.time() - age
    os.utime(path, (old, old))
    return path


class TestInbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "inbox")
        self.inbox = FileInbox(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_events_in_name_order(self):
        drop(self.dir, "002.json", {"id": "b", "thread": "t", "sender": "alice", "text": "second"})
        drop(self.dir, "001.json", {"id": "a", "thread": "t", "sender": "alice", "text": "first", "addressed": True})
        got = self.inbox.poll()
        self.assertEqual([t.text for t in got], ["first", "second"])
        self.assertTrue(got[0].addressed)
        self.assertFalse(got[1].addressed)

    def test_settle_moves_the_file_and_poll_is_then_empty(self):
        drop(self.dir, "1.json", {"id": "a", "text": "x"})
        t = self.inbox.poll()[0]
        self.assertEqual(len(self.inbox.poll()), 1, "polling must not consume the event")
        self.inbox.settle(t)
        self.assertEqual(self.inbox.poll(), [])
        self.assertEqual(len(os.listdir(self.inbox.done)), 1)

    def test_unreadable_file_is_quarantined_with_a_reason_not_dropped(self):
        """A malformed event must stay visible: silently deleting it would hide a lost request."""
        drop(self.dir, "bad.json", "{not json")
        drop(self.dir, "also-bad.json", {"id": "x"})                       # no text
        self.assertEqual(self.inbox.poll(), [])
        names = sorted(os.listdir(self.inbox.bad))
        self.assertIn("bad.json", names)
        self.assertIn("bad.json.why.txt", names)
        self.assertIn("also-bad.json", names)

    def test_a_file_still_being_written_is_left_alone(self):
        """A half-written event looks like broken JSON for a moment; quarantining it would lose a
        request that was only slow to arrive."""
        drop(self.dir, "young.json", '{"id": "a", "te', age=0.0)
        self.assertEqual(self.inbox.poll(), [])
        self.assertEqual(os.listdir(self.inbox.bad), [])
        drop(self.dir, "young.json", {"id": "a", "text": "now complete"}, age=0.0)
        self.assertEqual(len(self.inbox.poll()), 1)

    def test_thread_defaults_to_the_event_id(self):
        drop(self.dir, "1.json", {"id": "solo", "text": "x"})
        self.assertEqual(self.inbox.poll()[0].thread, "solo")

    def test_two_files_with_one_id_are_both_settled(self):
        """A copied file must not stay in the folder forever; the Store's UNIQUE key is what
        keeps it from becoming a second ticket."""
        drop(self.dir, "1.json", {"id": "same", "text": "x"})
        drop(self.dir, "2.json", {"id": "same", "text": "x"})
        got = self.inbox.poll()
        self.assertEqual(len(got), 2)
        self.inbox.settle(got[0])
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".json")], [])


class Crash(Exception):
    """Stands in for the orchestrator dying at an exact point."""


class TestOutbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self._tmp.name, "home")
        self.out = os.path.join(self._tmp.name, "out")
        self.store = Store(self.home).open()
        tid, _ = self.store.new_ticket("s", "t", Strength.EXPLICIT, lambda i: ("c", "s", "c"))
        self.tid = tid

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def files(self):
        return sorted(n for n in os.listdir(self.out) if not n.startswith("."))

    def read(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return json.load(f)

    def test_normal_path(self):
        ob = Outbox(self.store, FileSink(self.out))
        mid = ob.enqueue(self.tid, "hello")
        self.assertEqual(ob.flush(), 1)
        self.assertEqual(self.files(), [f"{mid}.json"])
        self.assertEqual(self.read(f"{mid}.json")["text"], "hello")
        self.assertEqual(self.read(f"{mid}.json")["msg_id"], mid)
        self.assertEqual(ob.flush(), 0, "a sent message must not be sent again")

    def test_crash_after_the_intent_is_committed_and_before_the_write(self):
        """The reply was decided and recorded, the process died, nothing was written. After the
        restart the reply must still go out, exactly once."""
        mid = Outbox(self.store, FileSink(self.out)).enqueue(self.tid, "decided")
        # (process dies here; a new Outbox over the same database is the restart)
        self.assertEqual(Outbox(self.store, FileSink(self.out)).flush(), 1)
        self.assertEqual(self.files(), [f"{mid}.json"])

    def test_crash_after_the_write_and_before_the_receipt(self):
        """The file exists but the database still says PENDING. Recovery writes it again; it
        must be the same file name with the same msg_id, so a consumer can tell it is a repeat."""
        ob = Outbox(self.store, FileSink(self.out))
        mid = ob.enqueue(self.tid, "once")
        real = self.store.outbox_mark

        def die(*a, **k):
            raise Crash()
        self.store.outbox_mark = die
        with self.assertRaises(Crash):
            ob.flush()
        self.store.outbox_mark = real
        self.assertEqual(self.files(), [f"{mid}.json"])
        before = self.read(f"{mid}.json")
        self.assertEqual(Outbox(self.store, FileSink(self.out)).flush(), 1)
        self.assertEqual(self.files(), [f"{mid}.json"])
        self.assertEqual(self.read(f"{mid}.json"), before)
        self.assertEqual(self.store.outbox_rows("sent")[0]["msg_id"], mid)

    def test_repeat_after_the_consumer_took_the_file_keeps_the_msg_id(self):
        """If the consumer already moved the first copy away, the recovery copy that reappears
        must carry the same msg_id: that is the whole de-duplication contract."""
        ob = Outbox(self.store, FileSink(self.out))
        mid = ob.enqueue(self.tid, "x")
        real = self.store.outbox_mark
        self.store.outbox_mark = lambda *a, **k: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            ob.flush()
        self.store.outbox_mark = real
        os.remove(os.path.join(self.out, f"{mid}.json"))                 # the consumer took it
        Outbox(self.store, FileSink(self.out)).flush()
        self.assertEqual(self.read(f"{mid}.json")["msg_id"], mid)

    def test_failing_sink_is_retried_then_marked_failed(self):
        """A reply that can never be delivered must show up as FAILED, not retry forever or
        vanish."""
        class Broken:
            def write(self, msg_id, payload):
                raise OSError("disk full")
        ob = Outbox(self.store, Broken())
        ob.enqueue(self.tid, "x")
        for attempt in range(1, MAX_ATTEMPTS):
            ob.flush()
            self.assertEqual(len(self.store.outbox_rows("pending")), 1, f"after attempt {attempt}")
        ob.flush()
        self.assertEqual(len(self.store.outbox_rows("failed")), 1)
        self.assertEqual(self.store.outbox_rows("failed")[0]["attempts"], MAX_ATTEMPTS)
        self.assertEqual(ob.flush(), 0)

    def test_file_name_carries_the_msg_id(self):
        """Without the id in the name a recovery write would make a second, differently named
        file for the same reply."""
        ob = Outbox(self.store, FileSink(self.out))
        ids = {ob.enqueue(self.tid, f"m{i}") for i in range(3)}
        ob.flush()
        self.assertEqual(set(n[:-5] for n in self.files()), ids)


if __name__ == "__main__":
    unittest.main()
