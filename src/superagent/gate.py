"""Admission: who may ask for work, and how strongly. Deterministic code; no model.

The model-based Assessor arrives in a later slice; here `RuleAssessor` is a keyword check so
the whole path can be tested without one.
"""
import dataclasses
import re
from typing import FrozenSet, Literal, Optional, Protocol, Tuple

from . import sanitize
from .model import Authorized, Decision, Strength, Trigger, Verdict

RULE_NAME = re.compile(r"^[a-z0-9-]{1,32}$")
RULE_MAX = 300
UNKNOWN_RULE_MAX = 64          # a rule name the model made up is kept this long, for the audit trail
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
    # A model answer that names a rule is asked about a second time, in a fresh session, and the
    # candidate is taken only if both answers name the same rule. Applies to `auto` and to `observe`
    # (whose `would_accept` is a preview of what `auto` would do); `off` never reaches the model.
    confirm_inferred: bool = True


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
    # The model's words go into the database and to a screen: cleaned first (a lone surrogate, which a
    # JSON "\ud800" escape produces, cannot be stored at all), cut after.
    why = sanitize.clean_line(reason)[:RULE_MAX] if isinstance(reason, str) else ""
    # The word "null" in quotes is the model's way of saying "no rule" (all 27 of the unavailable answers in
    # the 2026-11-06 re-run were exactly that). It goes only toward "do nothing", so it cannot open a ticket;
    # a rule that is really named "null" keeps working.
    # ponytail: one word, from evidence; "none"/"n/a" stay unknown names until someone sees them in the data
    if (isinstance(model_rule, str) and model_rule.strip().lower() == "null"
            and "null" not in {r.name for r in policy.rules}):
        model_rule = None
    if model_rule is None:
        return Authorized(Decision(Verdict.IGNORE, strength, f"model: {why}" if why else "model: no rule matches",
                                   model_rule=""))
    if not isinstance(model_rule, str) or model_rule not in {r.name for r in policy.rules}:
        # What the model wrote is kept for the audit only, cleaned first and cut after.
        said = sanitize.clean_line(model_rule)[:UNKNOWN_RULE_MAX]
        return Authorized(Decision(Verdict.UNAVAILABLE, strength, "unknown rule", model_rule=said))
    return _by_mode(model_rule, why, strength, policy)


def _by_mode(model_rule, why, strength, policy) -> Authorized:
    """A rule the policy knows, turned into a decision by the accept mode. `why` is already clean."""
    if policy.accept_mode == "auto":
        return Authorized(Decision(Verdict.ACCEPT, strength, f"rule {model_rule}: {why}", model_rule=model_rule),
                          (model_rule,))
    if policy.accept_mode == "observe":
        return Authorized(Decision(Verdict.IGNORE, strength, f"observe: would accept (rule {model_rule}): {why}",
                                   would_accept=True, model_rule=model_rule))
    return Authorized(Decision(Verdict.IGNORE, strength, "accept mode is off", model_rule=model_rule))


def needs_confirmation(first: Authorized, policy: Policy) -> bool:
    """True when the first answer named a rule the policy knows and a second look is due."""
    d = first.decision
    return (policy.confirm_inferred and policy.accept_mode in ("auto", "observe")
            and d.verdict is not Verdict.UNAVAILABLE and bool(d.model_rule))


def confirm(first_rule: str, first_why: str, second_rule, second_why, policy: Policy) -> Authorized:
    """The decision once the second answer is in. Both answers must name the same rule; anything
    else (no rule, another rule) is not taken. A name the policy does not know stays unavailable,
    as it does on a first look (the second answer goes through `authorize` first). The second
    answer's own rule is kept on the decision (the audit trail says what the model said last), and
    both reasons are kept, cleaned and cut."""
    second = authorize(second_rule, second_why, Strength.INFERRED, policy)
    d = second.decision
    if d.verdict is Verdict.UNAVAILABLE:
        return second
    clean = lambda t: sanitize.clean_line(t)[:100] if isinstance(t, str) else ""      # noqa: E731
    if d.model_rule == first_rule:
        return _by_mode(first_rule, f"confirmed twice (first: {clean(first_why)}; second: {clean(second_why)})",
                        d.strength, policy)
    said = d.model_rule or "no rule"
    return Authorized(Decision(Verdict.IGNORE, d.strength,
                               f"second look differs (first {first_rule}, second {said}): {clean(second_why)}",
                               model_rule=d.model_rule))
