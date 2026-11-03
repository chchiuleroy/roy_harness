"""Admission: who may ask for work, and how strongly. Deterministic code; no model.

The model-based Assessor arrives in a later slice; here `RuleAssessor` is a keyword check so
the whole path can be tested without one.
"""
import dataclasses
import re
from typing import FrozenSet, Literal, Optional, Protocol, Tuple

from .model import Authorized, Decision, Strength, Trigger, Verdict

RULE_NAME = re.compile(r"^[a-z0-9-]{1,32}$")
RULE_MAX = 300
_FORBIDDEN_IN_RULE = ("```", "[SUPERAGENT-", "[END-OF-")


@dataclasses.dataclass(frozen=True)
class Rule:
    """One kind of proactive work, in words. The name is what the model answers with, so it is
    a short fixed alphabet; the text goes into the model's instructions, so it must be a single
    plain line that cannot imitate the markers around outside text."""
    name: str
    instruction: str

    def __post_init__(self):
        if not isinstance(self.name, str) or not RULE_NAME.match(self.name):
            raise ValueError(f"rule name {self.name!r}: use 1-32 of a-z, 0-9 and -")
        text = self.instruction
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"rule {self.name}: the description is empty")
        if len(text) > RULE_MAX or "\n" in text or "\r" in text:
            raise ValueError(f"rule {self.name}: the description must be one line of at most {RULE_MAX} characters")
        if any(bad in text for bad in _FORBIDDEN_IN_RULE):
            raise ValueError(f"rule {self.name}: the description contains a reserved marker")


@dataclasses.dataclass(frozen=True)
class Policy:
    allowed_senders: FrozenSet[str] = frozenset()
    bot_senders: FrozenSet[str] = frozenset()
    accept_mode: Literal["off", "observe", "auto"] = "observe"     # observe is the default
    rules: Tuple[Rule, ...] = ()


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


def authorize(model_rule, reason, strength, policy) -> Authorized:
    """The only way an answer from the model becomes a decision.

    The model says which rule (if any) a candidate matches. Everything else is decided here: the
    rule must exist in the policy, the candidate must be an inferred one, and only `auto` mode
    turns a match into a ticket. A name that is not a known rule is `unavailable` (the model's
    words are not trusted, so they are not even kept); the model's reason is truncated and kept
    only as audit text, never as the grounds for the decision.
    """
    if strength is not Strength.INFERRED:
        raise ValueError("only inferred candidates are assessed by the model")
    why = reason[:RULE_MAX] if isinstance(reason, str) else ""
    if model_rule is None:
        return Authorized(Decision(Verdict.IGNORE, strength, f"model: {why}" if why else "model: no rule matches"))
    if not isinstance(model_rule, str) or model_rule not in {r.name for r in policy.rules}:
        return Authorized(Decision(Verdict.UNAVAILABLE, strength, "unknown rule"))
    if policy.accept_mode == "auto":
        return Authorized(Decision(Verdict.ACCEPT, strength, f"rule {model_rule}: {why}"), (model_rule,))
    if policy.accept_mode == "observe":
        return Authorized(Decision(Verdict.IGNORE, strength, f"observe: would accept (rule {model_rule}): {why}",
                                   would_accept=True))
    return Authorized(Decision(Verdict.IGNORE, strength, "accept mode is off"))
