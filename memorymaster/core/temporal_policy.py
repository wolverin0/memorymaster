"""Temporal truth and mutable operational-state admission policy.

Keeps time-bounded facts out of current recall and prevents temporary provider
or availability state from becoming durable global truth. Read when changing
claim validity, volatility, or scope governance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from memorymaster.core.models import _parse_iso_strict


class _TemporalClaim(Protocol):
    valid_from: str | None
    valid_until: str | None


@dataclass(frozen=True, slots=True)
class MutableStateDecision:
    is_mutable: bool
    volatility: str
    forced_high: bool = False


_MUTABLE_CLAIM_TYPES = {
    "",
    "constraint",
    "decision",
    "environment",
    "fact",
    "gotcha",
    "reference",
}
_RESOURCE_PATTERN = re.compile(
    r"\b(?:quota|credits?|credit balance|subscription (?:tier|plan)|free tier|"
    r"plan limits?|rate limits?|pricing|availability|installed state|on[- ]call|"
    r"role ownership)\b",
    re.IGNORECASE,
)
_TRANSIENT_STATE_PATTERN = re.compile(
    r"\b(?:currently|temporarily|until\s+\d{4}|exhausted|out of (?:credits?|quota)|"
    r"no longer|unavailable|down|not installed|is on[- ]call|owns? the role)\b",
    re.IGNORECASE,
)


def classify_mutable_state(
    *,
    text: str,
    claim_type: str | None,
    volatility: str,
    valid_until: str | None,
) -> MutableStateDecision:
    """Classify only narrow, explicit operational state; avoid topic-only hits."""
    normalized_type = (claim_type or "").strip().lower()
    is_mutable = (
        normalized_type in _MUTABLE_CLAIM_TYPES
        and _RESOURCE_PATTERN.search(text) is not None
        and _TRANSIENT_STATE_PATTERN.search(text) is not None
    )
    forced_high = bool(is_mutable and not valid_until and volatility != "high")
    return MutableStateDecision(
        is_mutable=is_mutable,
        volatility="high" if is_mutable and not valid_until else volatility,
        forced_high=forced_high,
    )


def claim_is_temporally_current(
    claim: _TemporalClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a claim is valid now; malformed persisted bounds fail closed."""
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    try:
        valid_from = _parse_iso_strict("valid_from", claim.valid_from)
        valid_until = _parse_iso_strict("valid_until", claim.valid_until)
    except ValueError:
        return False
    if valid_from is not None and valid_from > instant:
        return False
    return valid_until is None or valid_until > instant
