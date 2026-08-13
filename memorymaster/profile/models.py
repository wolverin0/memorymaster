"""Strict data contracts for compiled profile extraction and rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass

from memorymaster.core.security import scan_persisted_value


PROFILE_SCHEMA_VERSION = "memorymaster.compiled-profile.v1"
PROFILE_ALGORITHM_VERSION = "compiled-profile-map-reduce-v1"
PROFILE_CATEGORIES = (
    "identity_locale",
    "products_systems",
    "working_style",
    "standing_constraints",
)
PREDICATE_CATEGORY = {
    "location": "identity_locale",
    "timezone": "identity_locale",
    "currency": "identity_locale",
    "language": "identity_locale",
    "role": "identity_locale",
    "operates_product": "products_systems",
    "maintains_system": "products_systems",
    "works_in_domain": "products_systems",
    "primary_stack": "products_systems",
    "communication_style": "working_style",
    "workflow_preference": "working_style",
    "verification_preference": "working_style",
    "autonomy_preference": "working_style",
    "manual_work_constraint": "standing_constraints",
    "security_constraint": "standing_constraints",
    "deployment_constraint": "standing_constraints",
    "tooling_constraint": "standing_constraints",
}
PROFILE_VOLATILITIES = ("stable", "preference")
PROFILE_ACTIONS = ("add", "reinforce", "replace", "ignore")
_INSTRUCTION = re.compile(
    r"(?i)\b(?:you|assistant|agents?|claude|codex)\b.{0,50}\b(?:must|should|always|never)\b"
)


class ProfileValidationError(ValueError):
    """Profile provider output failed a deterministic boundary."""


def validate_fact_fields(category: str, predicate: str, value: str, volatility: str) -> None:
    if category not in PROFILE_CATEGORIES:
        raise ProfileValidationError("unknown profile category")
    if PREDICATE_CATEGORY.get(predicate) != category:
        raise ProfileValidationError("unknown or mismatched profile predicate")
    if volatility not in PROFILE_VOLATILITIES:
        raise ProfileValidationError("unknown profile volatility")
    if not value.strip() or len(value.strip()) > 240 or "\n" in value:
        raise ProfileValidationError("profile value is malformed or oversized")
    if _INSTRUCTION.search(value):
        raise ProfileValidationError("profile value is instruction-shaped")
    if scan_persisted_value(value):
        raise ProfileValidationError("profile value contains sensitive material")


@dataclass(frozen=True, slots=True)
class ProfileMessage:
    message_id: int
    session_id: str
    scope: str
    text: str
    assistant_context: str


@dataclass(frozen=True, slots=True)
class ProfileCandidate:
    candidate_id: str
    category: str
    predicate: str
    value: str
    volatility: str
    support_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        validate_fact_fields(self.category, self.predicate, self.value, self.volatility)
        if not self.candidate_id or len(self.candidate_id) > 120:
            raise ProfileValidationError("candidate id is malformed")
        if not self.support_ids or len(self.support_ids) > 20:
            raise ProfileValidationError("candidate supports are malformed")
        if len(set(self.support_ids)) != len(self.support_ids):
            raise ProfileValidationError("candidate supports contain duplicates")


@dataclass(frozen=True, slots=True)
class ProfileDecision:
    candidate_ids: tuple[str, ...]
    action: str
    category: str = ""
    predicate: str = ""
    value: str = ""
    volatility: str = "stable"
    target_fact_id: int | None = None
    confidence: float = 0.0
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.action not in PROFILE_ACTIONS or not self.candidate_ids:
            raise ProfileValidationError("profile decision action or candidates are invalid")
        if self.action in {"add", "replace"}:
            validate_fact_fields(self.category, self.predicate, self.value, self.volatility)
        if self.action in {"reinforce", "replace"} and not self.target_fact_id:
            raise ProfileValidationError("profile decision target is required")
        if not 0.0 <= float(self.confidence) <= 1.0 or len(self.rationale) > 500:
            raise ProfileValidationError("profile decision metadata is invalid")


@dataclass(frozen=True, slots=True)
class ProfileFact:
    fact_id: int
    category: str
    predicate: str
    value: str
    volatility: str
    status: str
    support_hash: str
    support_count: int
    independent_sessions: int
    first_seen_at: str
    last_supported_at: str
    support_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class MessageBatch:
    messages: tuple[ProfileMessage, ...]
    scanned_through_id: int


@dataclass(frozen=True, slots=True)
class RenderedProfile:
    markdown: str
    fact_ids: tuple[int, ...]
    tokens_used: int
