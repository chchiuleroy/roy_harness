"""Plain data used across the package. No behaviour, no I/O."""
import dataclasses
import enum
from typing import Optional, Tuple


class Strength(enum.Enum):
    EXPLICIT = "explicit"              # an allowed person asks for work
    INFERRED = "inferred"              # a candidate; only observe/auto modes may act on it
    NOT_ADMITTED = "not_admitted"


class Verdict(enum.Enum):
    ACCEPT = "accept"
    IGNORE = "ignore"
    STOP = "stop"
    UNAVAILABLE = "unavailable"
    FANOUT = "fanout"                  # a follow-up on a thread that already has a live ticket: no Eval
    REOPEN = "reopen"                  # a message on a thread whose ticket had ended: that same ticket is open again


class TicketState(enum.Enum):
    OPEN = "open"
    WAITING = "waiting"                # the harness asked a question; only a person can answer
    ENDED = "ended"
    STOPPED = "stopped"                # a person stopped it; it receives nothing more


class InputState(enum.Enum):
    PENDING = "pending"
    SENT = "sent"                      # committed, then written to the child's stdin
    DONE = "done"
    REPLAY = "replay"                  # sent before a restart; may or may not have been handled


class AskState(enum.Enum):
    OPEN = "open"
    ANSWERED = "answered"
    ORPHANED = "orphaned"              # its process is gone; no answer can be accepted any more


@dataclasses.dataclass(frozen=True)
class Trigger:
    source: str
    ext_id: str
    thread: str
    sender: str
    text: str
    addressed: bool = False            # does it speak to the agent directly
    ts: float = 0.0


@dataclasses.dataclass(frozen=True)
class Decision:
    verdict: Verdict
    strength: Strength
    reason: str
    would_accept: bool = False         # observe mode: an inferred candidate that auto would take
    # What the model said, for the audit trail (never the grounds for a decision):
    #   None  no model answer (rule-based decision, explicit request, a candidate given up on)
    #   ""    the model said no rule applies
    #   name  the rule the model named; a name the policy does not know is kept cleaned and cut to 64 characters
    model_rule: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class Mandate:
    ticket_id: int
    root: str
    strength: Strength
    source: str
    thread: str
    tools_allowed: Tuple[str, ...] = ("read_file", "write_file", "edit_file", "run_command",
                                      "todo_write")
    matched_rules: Tuple[str, ...] = ()        # which proactive rule(s) allowed it, if it was not an explicit request


@dataclasses.dataclass(frozen=True)
class Authorized:
    """A decision about a candidate that came out of `gate.authorize`. The store accepts nothing
    else for a model-assessed candidate, so there is no way to hand it a hand-made Decision."""
    decision: Decision
    matched_rules: Tuple[str, ...] = ()
