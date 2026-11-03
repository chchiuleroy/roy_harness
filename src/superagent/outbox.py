"""Replies that survive a crash.

State per message: PENDING -> SENT, or FAILED after MAX_ATTEMPTS. The row is committed before
anything is written (so a crash can never lose a reply it already decided to send), the write
is idempotent by msg_id (so redoing it after a crash is harmless), and `flush()` is what
recovery runs: it simply redoes every PENDING row.
"""
import json

MAX_ATTEMPTS = 3


class Outbox:
    def __init__(self, store, sink):
        self.store, self.sink = store, sink

    def enqueue(self, ticket_id, text):
        """Commit the intent to reply. Nothing is written yet. -> msg_id"""
        _, msg_id = self.store.outbox_enqueue(ticket_id, {"ticket_id": ticket_id, "text": text})
        return msg_id

    def flush(self):
        """Redo every PENDING row. -> number sent in this call."""
        sent = 0
        for row in self.store.outbox_rows("pending"):
            try:
                self.sink.write(row["msg_id"], json.loads(row["payload"]))
            except OSError:
                attempts = row["attempts"] + 1
                self.store.outbox_mark(row["id"], "failed" if attempts >= MAX_ATTEMPTS else "pending",
                                       attempts)
                continue
            self.store.outbox_mark(row["id"], "sent")
            sent += 1
        return sent
