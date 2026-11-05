"""What a harness process said at the end of a turn, read from its event stream.

One rule, used for the model session (Slice 2) and for ticket executors (Slice 3): the reply of a
turn is the text the model wrote AFTER its last tool event in that turn. Text from before a tool
call is void (it was thinking aloud, and the answer comes after the tools have run).
"""
import dataclasses

from . import sanitize

REPLY_CAP = 4000          # characters of a reply that leave the system
DETAIL_CAP = 300          # characters of a failure reason that leave the system
FAILURE_MARK = "model call failed"       # what harness_vllm prints when it gives up on a turn


@dataclasses.dataclass(frozen=True)
class TurnEnd:
    text: "str | None"    # None: the turn ended without any text after its last tool event
    failure: "str | None" = None    # the harness' own line when the model call failed in this turn


class ReplyTracker:
    """Fed every event of ONE harness process, in order."""

    def __init__(self):
        self.text = None
        self.failure = None

    def reset(self):
        self.text = None
        self.failure = None

    def feed(self, ev):
        """-> a TurnEnd when this event ends a turn, else None."""
        kind = ev.get("kind")
        if kind == "assistant":
            self.text = str(ev.get("text", ""))
        elif kind in ("turn_start", "tool_call", "tool_result"):
            self.text = None
            if kind == "turn_start":
                self.failure = None
        elif kind == "log":
            if FAILURE_MARK in str(ev.get("text", "")):
                self.failure = str(ev["text"]).strip()
        elif kind == "turn_end":
            text, self.text = self.text, None
            failure, self.failure = self.failure, None
            return TurnEnd(text, failure if not (text and text.strip()) else None)
        return None


def reply_payload(ticket_id, input_id, text, failure=None):
    """The outbox payload for a turn that handled `input_id`. The model's words are DATA in one
    field, marked untrusted; a consumer must not treat them as a command, a markup action, a routing
    field or a control message for another agent. A turn with no text still gets one message, so a
    failed model call is not silence. When the harness said why (`failure`), the reason rides along as
    `detail`: one cleaned, shortened line of the endpoint's own words, marked untrusted like a reply, and the
    sentence in `text` stays fixed."""
    if text is None or not text.strip():
        if failure:
            lines = str(failure).strip().splitlines()
            detail = sanitize.clean_line(lines[0])[:DETAIL_CAP].strip() if lines else ""
            if detail:
                return {"kind": "status", "ticket_id": ticket_id, "input_id": input_id, "untrusted": True,
                        "text": f"Ticket {ticket_id} finished a turn without a reply: the model call failed "
                                f"(see detail).",
                        "detail": detail}
        return {"kind": "status", "ticket_id": ticket_id, "input_id": input_id,
                "text": f"Ticket {ticket_id} finished a turn without a reply (see its folder)."}
    cleaned, dropped = clean_reply(text)
    return {"kind": "executor_output", "untrusted": True, "ticket_id": ticket_id, "input_id": input_id,
            "text": cleaned, "truncated_chars": dropped}


def clean_reply(text, cap=REPLY_CAP):
    """-> (text, characters_dropped). A reply is data for a reader, not something that can steer a
    terminal, reorder what is shown or break a file write: control characters (the C0 and C1
    ranges and DEL, except newline and tab), format characters that change how text is drawn
    (bidirectional overrides and isolates and the like; the joiners are kept), lone surrogates (they
    cannot be written as UTF-8) and the Unicode line and paragraph separators are removed. Then the
    text is cut to `cap` characters."""
    text = sanitize.clean(text)
    return text[:cap], max(0, len(text) - cap)
