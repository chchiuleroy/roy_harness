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
        _, msg_id = self.store.outbox_enqueue(ticket_id, {"kind": "status", "ticket_id": ticket_id, "text": text})
        return msg_id

    def flush(self):
        """Redo every PENDING row. -> number sent in this call."""
        sent = 0
        for row in self.store.outbox_rows("pending"):
            try:
                payload = json.loads(row["payload"])
                if not isinstance(payload, dict):
                    raise ValueError("the payload is not an object")
                payload.setdefault("kind", "status")      # rows queued before `kind` existed were all status messages
                self.sink.write(row["msg_id"], payload)
            except (OSError, ValueError):                 # a row that cannot be read or written (damaged JSON, a text the
                                                          # file cannot hold) is a failed try, never a crash that blocks the rest
                attempts = row["attempts"] + 1
                self.store.outbox_mark(row["id"], "failed" if attempts >= MAX_ATTEMPTS else "pending",
                                       attempts)
                continue
            self.store.outbox_mark(row["id"], "sent")
            sent += 1
        return sent
