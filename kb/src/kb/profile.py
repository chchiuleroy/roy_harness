"""Collection profiles (Gate 1 decision 5, Gate 2 section 4.4).

A profile is per-scenario privacy policy. The values are Roy's to choose per collection later; what this module guarantees
now is (1) conservative defaults, (2) an invariant that 'secret' cannot be loosened by individual flags, (3) strict loading
(unknown keys are errors, so a typo cannot silently weaken a policy), and (4) the five enforcement points as functions that
raise PolicyViolation. Modules added in later stages must call them; tests at those stages assert that they do.
"""
import json
from dataclasses import asdict, dataclass
from typing import Optional, Tuple

from .canonical import canonical_bytes
from .errors import KbError, PolicyViolation

SENSITIVITIES = ("public", "private", "secret")


@dataclass(frozen=True)
class Profile:
    sensitivity: str = "private"
    index_plaintext: bool = True
    allow_external_model: bool = False
    agent_visible: bool = False
    retention_days: Optional[int] = None
    allowed_converters: Optional[Tuple[str, ...]] = None      # None means every installed converter

    def __post_init__(self):
        if self.sensitivity not in SENSITIVITIES:
            raise KbError(f"sensitivity must be one of {SENSITIVITIES}")
        for name in ("index_plaintext", "allow_external_model", "agent_visible"):
            if not isinstance(getattr(self, name), bool):
                raise KbError(f"{name} must be a boolean")
        if self.retention_days is not None and (not isinstance(self.retention_days, int) or isinstance(self.retention_days, bool)
                                                or self.retention_days <= 0):
            raise KbError("retention_days must be a positive integer or null")
        if self.allowed_converters is not None:
            if not isinstance(self.allowed_converters, tuple) or not all(isinstance(c, str) and c for c in self.allowed_converters):
                raise KbError("allowed_converters must be null or a tuple of converter names")
        if self.sensitivity == "secret" and (self.index_plaintext or self.allow_external_model or self.agent_visible):
            raise KbError("a 'secret' collection cannot be indexed in plaintext, sent to an external model or shown to an agent")

    def to_json(self):
        d = asdict(self)
        if d["allowed_converters"] is not None:
            d["allowed_converters"] = list(d["allowed_converters"])
        return canonical_bytes(d).decode("utf-8")

    @classmethod
    def from_json(cls, text):
        try:
            d = json.loads(text)
        except ValueError as e:
            raise KbError(f"profile is not valid JSON: {e}") from e
        if not isinstance(d, dict):
            raise KbError("profile must be a JSON object")
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise KbError(f"unknown profile key(s) {sorted(unknown)}")
        conv = d.get("allowed_converters")
        if conv is not None:
            if not isinstance(conv, list):                      # tuple("pdf") would silently become ('p','d','f')
                raise KbError("allowed_converters must be null or a list of converter names")
            d["allowed_converters"] = tuple(conv)
        return cls(**d)


# ---- the five enforcement points ------------------------------------------------------------------------------
def require_convert(profile, converter_name):
    """(1) before a converter runs on a source of this collection."""
    if profile.allowed_converters is not None and converter_name not in profile.allowed_converters:
        raise PolicyViolation(f"converter {converter_name!r} is not allowed for this collection")


def require_index(profile):
    """(2) before text of this collection enters any index (FTS or vectors)."""
    if not profile.index_plaintext:
        raise PolicyViolation("this collection must not be indexed in plaintext")


def require_external_model(profile):
    """(3) before any content of this collection is handed to a model or service outside this machine."""
    if not profile.allow_external_model:
        raise PolicyViolation("this collection must not be sent to an external model")


def require_agent_visible(profile):
    """(4) before any content of this collection is returned to an agent."""
    if not profile.agent_visible:
        raise PolicyViolation("this collection is not visible to agents")


# (5) the audit log accepts identifiers only: see audit.py
