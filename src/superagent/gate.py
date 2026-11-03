"""Admission: who may ask for work, and how strongly. Deterministic code; no model.

The model-based Assessor arrives in a later slice; here `RuleAssessor` is a keyword check so
the whole path can be tested without one.
"""
import dataclasses
from typing import FrozenSet, Literal, Protocol, Tuple

from .model import Decision, Strength, Trigger, Verdict


@dataclasses.dataclass(frozen=True)
class Policy:
    allowed_senders: FrozenSet[str] = frozenset()
    bot_senders: FrozenSet[str] = frozenset()
    accept_mode: Literal["off", "observe", "auto"] = "observe"     # observe is the default


def admit(t: Trigger, p: Policy, has_live_ticket_on_thread: bool = False) -> Strength:
    """How strong the basis for acting on this message is.

    An allowed person who addresses the agent, or who follows up on a thread that already has a
    ticket, makes an explicit request. A bot is never more than inferred. Unlisted people are
    ignored unless they follow up on a live thread, and then they are only inferred. While the
    accept mode is `off`, every inferred case is not admitted.
    """
    if t.sender in p.bot_senders:
        strength = Strength.INFERRED
    elif t.sender in p.allowed_senders:
        strength = (Strength.EXPLICIT if t.addressed or has_live_ticket_on_thread
                    else Strength.INFERRED)
    elif has_live_ticket_on_thread:
        strength = Strength.INFERRED
    else:
        strength = Strength.NOT_ADMITTED
    if strength is Strength.INFERRED and p.accept_mode == "off":
        return Strength.NOT_ADMITTED
    return strength


class Assessor(Protocol):
    def assess(self, t: Trigger, strength: Strength) -> Tuple[bool, str]:
        """(does it match a rule for proactive work, why)"""


class RuleAssessor:
    def __init__(self, keywords=()):
        self.keywords = tuple(k.lower() for k in keywords)

    def assess(self, t, strength):
        for k in self.keywords:
            if k in t.text.lower():
                return True, f"matches rule {k!r}"
        return False, "no rule matches"


def evaluate(t: Trigger, p: Policy, assessor: Assessor, has_live_ticket_on_thread=False) -> Decision:
    """The decision for a trigger that is not a follow-up already owned by a ticket."""
    strength = admit(t, p, has_live_ticket_on_thread)
    if strength is Strength.NOT_ADMITTED:
        return Decision(Verdict.IGNORE, strength, "not admitted")
    if strength is Strength.EXPLICIT:                 # explicit requests work in every mode
        return Decision(Verdict.ACCEPT, strength, "explicit request")
    matches, why = assessor.assess(t, strength)
    if not matches:
        return Decision(Verdict.IGNORE, strength, why)
    if p.accept_mode == "auto":
        return Decision(Verdict.ACCEPT, strength, why)
    return Decision(Verdict.IGNORE, strength, f"observe: would accept ({why})", would_accept=True)
