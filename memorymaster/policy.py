from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from memorymaster.models import Claim

POLICY_MODES = ("legacy", "cadence")

# Base hours by volatility. Claim-type and state multipliers tune the final cadence.
_BASE_CADENCE_HOURS = {
    "low": 168.0,
    "medium": 72.0,
    "high": 24.0,
}

_CLAIM_TYPE_MULTIPLIER = {
    "security_fact": 0.5,
    "infra_fact": 0.7,
    "filesystem_fact": 1.0,
}

_STATE_MULTIPLIER = {
    "confirmed": 1.0,
    "stale": 0.55,
    "conflicted": 0.35,
}


@dataclass(slots=True)
class RevalidationSelection:
    mode: str
    considered: int
    due: int
    selected: list[Claim]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _base_cadence_hours(claim: Claim) -> float:
    base = _BASE_CADENCE_HOURS.get(claim.volatility, _BASE_CADENCE_HOURS["medium"])
    type_mult = _CLAIM_TYPE_MULTIPLIER.get(claim.claim_type or "", 1.0)
    state_mult = _STATE_MULTIPLIER.get(claim.status, 1.0)
    return max(1.0, base * type_mult * state_mult)


def _age_seconds(claim: Claim, now: datetime) -> float:
    anchor = _parse_iso(claim.last_validated_at) or _parse_iso(claim.updated_at) or _parse_iso(claim.created_at)
    if anchor is None:
        return 0.0
    return max(0.0, (now - anchor).total_seconds())


def _priority_score(claim: Claim, age_seconds: float, cadence_seconds: float) -> float:
    overdue_ratio = age_seconds / max(1.0, cadence_seconds)
    status_bonus = 0.25 if claim.status == "stale" else (0.4 if claim.status == "conflicted" else 0.0)
    confidence_bonus = (1.0 - max(0.0, min(1.0, claim.confidence))) * 0.2
    volatility_bonus = 0.2 if claim.volatility == "high" else (0.1 if claim.volatility == "medium" else 0.0)
    return overdue_ratio + status_bonus + confidence_bonus + volatility_bonus


def select_revalidation_candidates(
    store,
    *,
    mode: str = "legacy",
    limit: int = 200,
) -> RevalidationSelection:
    if mode not in POLICY_MODES:
        raise ValueError(f"Unknown policy mode: {mode}")
    if mode == "legacy":
        return RevalidationSelection(mode=mode, considered=0, due=0, selected=[])
    if limit <= 0:
        return RevalidationSelection(mode=mode, considered=0, due=0, selected=[])

    fetch_limit = max(limit * 4, 200)
    pool = store.list_claims(
        status_in=["confirmed", "stale", "conflicted"],
        limit=fetch_limit,
        include_archived=False,
        include_citations=False,
    )

    now = datetime.now(timezone.utc)
    due_rows: list[tuple[float, Claim]] = []

    for claim in pool:
        cadence_seconds = _base_cadence_hours(claim) * 3600.0
        age_seconds = _age_seconds(claim, now)
        if age_seconds < cadence_seconds:
            continue
        score = _priority_score(claim, age_seconds, cadence_seconds)
        due_rows.append((score, claim))

    due_rows.sort(
        key=lambda row: (
            row[0],
            row[1].pinned,
            row[1].confidence,
            row[1].updated_at,
            row[1].id,
        ),
        reverse=True,
    )
    selected = [claim for _, claim in due_rows[:limit]]
    return RevalidationSelection(mode=mode, considered=len(pool), due=len(due_rows), selected=selected)
