from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math

from memorymaster.core.security import is_sensitive_claim


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


def _build_reason(status: str, *, flagged: bool = False) -> str:
    parts: list[str] = []
    if status in ("conflicted", "stale"):
        parts.append(f"status={status}")
    if flagged:
        parts.append("pending steward proposal")
    if not parts:
        parts.append(f"status={status}")
    return ",".join(parts)


def _reviewable_claims(service, *, limit: int, include_stale: bool, include_conflicted: bool, flagged_claim_ids: set[int]) -> list:
    """Solo lo que NECESITA revision humana: stale, conflicted, o con propuesta.

    La version anterior tenia el filtro INVERTIDO: escaneaba los primeros N
    claims de CUALQUIER estado y solo permitia excluir stale/conflicted. El
    resultado, visto por el operador el 2026-08-26 en el dashboard: una cola
    titulada "claims that need human review" llena de claims confirmed cuya
    "razon" era `status=confirmed` — una no-razon — mientras los 5.976
    conflicted reales quedaban fuera del escaneo. Una cola de revision donde
    los items no dicen por que estan es la misma familia que la senal verde
    que no ejerce su camino: entrena a ignorar la cola.
    """
    store = getattr(service, "store", None)
    finder = getattr(store, "find_by_status", None)
    gathered: list = []
    if finder is not None:
        if include_stale:
            gathered.extend(finder("stale", limit=limit, include_citations=True))
        if include_conflicted:
            gathered.extend(finder("conflicted", limit=limit, include_citations=True))
        getter = getattr(store, "get_claim", None)
        if getter is not None:
            present = {claim.id for claim in gathered}
            for claim_id in sorted(flagged_claim_ids - present)[:limit]:
                claim = getter(claim_id, include_citations=True)
                if claim is not None:
                    gathered.append(claim)
        return gathered
    # Respaldo para dobles de test sin store: el escaneo viejo, pero FILTRANDO.
    wanted = {"stale"} if include_stale else set()
    if include_conflicted:
        wanted.add("conflicted")
    return [
        claim
        for claim in service.list_claims(include_archived=False, limit=limit, allow_sensitive=True)
        if claim.status in wanted or claim.id in flagged_claim_ids
    ]


def build_review_queue(
    service,
    *,
    limit: int = 100,
    include_stale: bool = True,
    include_conflicted: bool = True,
    include_sensitive: bool = False,
    flagged_claim_ids: set[int] | None = None,
) -> list[ReviewItem]:
    if limit <= 0:
        return []

    flagged = flagged_claim_ids or set()
    claims = _reviewable_claims(
        service,
        limit=limit,
        include_stale=include_stale,
        include_conflicted=include_conflicted,
        flagged_claim_ids=flagged,
    )
    now = datetime.now(timezone.utc)
    items: list[ReviewItem] = []
    seen: set[int] = set()

    for claim in claims:
        if claim.id in seen:
            continue
        seen.add(claim.id)
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
                reason=_build_reason(claim.status, flagged=claim.id in flagged),
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


def build_candidate_backlog_plan(
    service,
    *,
    daily_capacity: int = 688,
    batch_size: int = 100,
    scan_limit: int = 25_000,
) -> dict[str, object]:
    """Return a bounded, read-only candidate review plan."""
    if daily_capacity <= 0 or batch_size <= 0 or scan_limit <= 0:
        raise ValueError("daily_capacity, batch_size, and scan_limit must be positive")
    claims = service.list_claims(
        status="candidate",
        include_archived=False,
        limit=scan_limit,
        allow_sensitive=False,
    )
    count = len(claims)
    return {
        "dry_run": True,
        "candidate_count": count,
        "scan_limit": scan_limit,
        "truncated": count >= scan_limit,
        "daily_capacity": daily_capacity,
        "batch_size": batch_size,
        "review_batches": math.ceil(count / batch_size),
        "minimum_days": math.ceil(count / daily_capacity),
        "automatic_transitions": 0,
        "candidate_ids": [int(claim.id) for claim in claims[:batch_size]],
    }
