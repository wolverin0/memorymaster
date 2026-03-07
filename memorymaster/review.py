from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math

from memorymaster.security import is_sensitive_claim


@dataclass(slots=True)
class ReviewItem:
    claim_id: int
    status: str
    subject: str | None
    predicate: str | None
    object_value: str | None
    confidence: float
    updated_at: str
    reason: str
    priority: float
    citations_count: int


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


def _priority_score(*, status: str, confidence: float, updated_at: str, now: datetime) -> float:
    status_weight = {
        "conflicted": 0.55,
        "stale": 0.40,
    }.get(status, 0.15)

    bounded_confidence = max(0.0, min(1.0, confidence))
    confidence_weight = (1.0 - bounded_confidence) * 0.35

    recency_weight = 0.0
    updated = _parse_iso(updated_at)
    if updated is not None:
        age_hours = max(0.0, (now - updated).total_seconds() / 3600.0)
        recency_weight = 0.20 * math.exp(-age_hours / 48.0)

    return status_weight + confidence_weight + recency_weight


def _build_reason(status: str) -> str:
    parts: list[str] = []
    if status == "conflicted":
        parts.append("status=conflicted")
    if status == "stale":
        parts.append("status=stale")
    if not parts:
        parts.append(f"status={status}")
    return ",".join(parts)


def build_review_queue(
    service,
    *,
    limit: int = 100,
    include_stale: bool = True,
    include_conflicted: bool = True,
    include_sensitive: bool = False,
) -> list[ReviewItem]:
    if limit <= 0:
        return []

    claims = service.list_claims(include_archived=False, limit=limit, allow_sensitive=include_sensitive)
    now = datetime.now(timezone.utc)
    items: list[ReviewItem] = []

    for claim in claims:
        if claim.status == "stale" and not include_stale:
            continue
        if claim.status == "conflicted" and not include_conflicted:
            continue
        if not include_sensitive and is_sensitive_claim(claim):
            continue

        items.append(
            ReviewItem(
                claim_id=claim.id,
                status=claim.status,
                subject=claim.subject,
                predicate=claim.predicate,
                object_value=claim.object_value,
                confidence=claim.confidence,
                updated_at=claim.updated_at,
                reason=_build_reason(claim.status),
                priority=_priority_score(
                    status=claim.status,
                    confidence=claim.confidence,
                    updated_at=claim.updated_at,
                    now=now,
                ),
                citations_count=len(claim.citations),
            )
        )

    min_dt = datetime.min.replace(tzinfo=timezone.utc)
    items.sort(
        key=lambda item: (
            item.priority,
            _parse_iso(item.updated_at) or min_dt,
            item.claim_id,
        ),
        reverse=True,
    )
    return items


def queue_to_dicts(items: list[ReviewItem]) -> list[dict[str, object]]:
    return [asdict(item) for item in items]
