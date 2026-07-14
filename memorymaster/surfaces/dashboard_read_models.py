"""Read-only application models for dashboard HTTP handlers."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from memorymaster.core.security import is_sensitive_claim
from memorymaster.govern.review import build_review_queue, queue_to_dicts
from memorymaster.govern.steward import list_steward_proposals


Serializer = Callable[[Any], dict[str, Any]]


def triage_flags(service: Any, limit: int) -> dict[int, dict[str, bool]]:
    flags: dict[int, dict[str, bool]] = {}
    for event in reversed(service.list_events(limit=limit, event_type="audit")):
        if event.claim_id is None:
            continue
        ref = flags.setdefault(int(event.claim_id), {"reviewed": False, "suppressed": False})
        details = str(event.details or "")
        if details == "triage_mark_reviewed":
            ref["reviewed"] = True
        elif details == "triage_suppress":
            ref["suppressed"] = True
        elif details == "triage_unsuppress":
            ref["suppressed"] = False
    return flags


def claims_payload(
    service: Any,
    *,
    status: str | None,
    limit: int,
    include_archived: bool,
    allow_sensitive: bool,
    serialize: Serializer,
) -> dict[str, Any]:
    claims = service.list_claims(status=status, limit=limit, include_archived=include_archived)
    if not allow_sensitive:
        claims = [claim for claim in claims if not is_sensitive_claim(claim)]
    return {"ok": True, "rows": len(claims), "claims": [serialize(claim) for claim in claims]}


def events_payload(
    service: Any,
    *,
    limit: int,
    claim_id: int | None,
    event_type: str | None,
    serialize: Serializer,
    output_key: str = "events",
) -> dict[str, Any]:
    events = service.list_events(claim_id=claim_id, limit=limit, event_type=event_type)
    return {"ok": True, "rows": len(events), output_key: [serialize(event) for event in events]}


def conflicts_payload(
    service: Any,
    *,
    limit: int,
    include_stale: bool,
    serialize: Serializer,
) -> dict[str, Any]:
    conflicted = service.list_claims(status="conflicted", limit=limit, include_archived=False)
    if not conflicted:
        return {"ok": True, "rows": 0, "groups": []}
    statuses = ["confirmed", "conflicted"] + (["stale"] if include_stale else [])
    active = service.store.list_claims(
        limit=max(limit * 12, 200), status_in=statuses,
        include_archived=False, include_citations=True,
    )
    grouped: dict[tuple[str, str, str], list[Any]] = {}
    for claim in conflicted:
        grouped.setdefault(_claim_key(claim), [])
    for claim in active:
        if _claim_key(claim) in grouped:
            grouped[_claim_key(claim)].append(claim)
    groups = [_conflict_group(key, claims, serialize) for key, claims in grouped.items()]
    groups.sort(key=lambda item: (item["subject"], item["predicate"], item["scope"]))
    return {"ok": True, "rows": len(groups), "groups": groups}


def _claim_key(claim: Any) -> tuple[str, str, str]:
    return (
        str(claim.subject or ""),
        str(claim.predicate or ""),
        str(claim.scope or "project"),
    )


def _conflict_group(key: tuple[str, str, str], claims: list[Any], serialize: Serializer) -> dict[str, Any]:
    ordered = sorted(claims, key=lambda claim: (str(claim.updated_at), int(claim.id)), reverse=True)
    return {
        "subject": key[0], "predicate": key[1], "scope": key[2],
        "claims": [serialize(claim) for claim in ordered],
    }


def review_queue_payload(
    service: Any,
    *,
    limit: int,
    include_stale: bool,
    include_conflicted: bool,
    allow_sensitive: bool,
    exclude_reviewed: bool,
    exclude_suppressed: bool,
) -> dict[str, Any]:
    items = build_review_queue(
        service, limit=limit, include_stale=include_stale,
        include_conflicted=include_conflicted, include_sensitive=allow_sensitive,
    )
    flags = triage_flags(service, max(limit * 20, 200))
    out = _apply_triage_filters(queue_to_dicts(items), flags, exclude_reviewed, exclude_suppressed)
    proposals = _pending_proposals_by_claim(service, limit)
    out = [{**item, "proposal": proposals.get(int(item["claim_id"]))} for item in out]
    return {"ok": True, "rows": len(out), "items": out}


def _pending_proposals_by_claim(service: Any, limit: int) -> dict[int, dict[str, Any]]:
    proposals = list_steward_proposals(service, limit=max(limit, 1), include_resolved=False)
    latest: dict[int, dict[str, Any]] = {}
    for proposal in proposals:
        claim_id = proposal.get("claim_id")
        if not isinstance(claim_id, int):
            continue
        current = latest.get(claim_id)
        if current is None or int(proposal["proposal_event_id"]) > int(current["proposal_event_id"]):
            latest[claim_id] = proposal
    return latest


def _apply_triage_filters(
    items: list[dict[str, Any]],
    flags: dict[int, dict[str, bool]],
    exclude_reviewed: bool,
    exclude_suppressed: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        triage = flags.get(int(item["claim_id"]), {"reviewed": False, "suppressed": False})
        item = {**item, "reviewed": bool(triage["reviewed"]), "suppressed": bool(triage["suppressed"])}
        if (exclude_reviewed and item["reviewed"]) or (exclude_suppressed and item["suppressed"]):
            continue
        out.append(item)
    return out


def mobile_review_queue_payload(
    service: Any, *, limit: int, scope: str | None, cursor: int | None
) -> dict[str, Any]:
    clauses, params = ["archived_at IS NULL"], []
    if scope is not None:
        clauses.append("scope = ?")
        params.append(scope)
    if cursor is not None:
        clauses.append("id > ?")
        params.append(cursor)
    sql = _mobile_queue_sql(" AND ".join(clauses))
    with service.store.connect() as conn:
        rows = conn.execute(sql, [*params, limit + 1]).fetchall()
    page = rows[:limit]
    queue = [_mobile_queue_row(row) for row in page]
    next_cursor = int(page[-1]["id"]) if len(rows) > limit and page else None
    return {"queue": queue, "cursor": next_cursor}


def _mobile_queue_sql(where: str) -> str:
    return f"""SELECT id, text, created_at, scope, claim_type, confidence
        FROM claims WHERE {where}
        ORDER BY created_at ASC, id ASC LIMIT ?"""


def _mobile_queue_row(row: Any) -> dict[str, Any]:
    normalized = str(row["created_at"]).strip().replace("Z", "+00:00")
    created = datetime.fromisoformat(normalized)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = max(0.0, (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 86400.0)
    return {
        "id": int(row["id"]), "text_preview": str(row["text"] or "")[:200],
        "age_days": age, "scope": row["scope"], "type": row["claim_type"],
        "score": float(row["confidence"]),
    }


def action_proposals_payload(
    service: Any, *, status: str | None, destination: str | None, limit: int
) -> dict[str, Any]:
    proposals = service.list_action_proposals(status=status, destination=destination, limit=limit)
    return {"ok": True, "rows": len(proposals), "proposals": [asdict(item) for item in proposals]}


def audit_payload(service: Any, *, limit: int, serialize: Serializer) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for event_type in ("audit", "policy_decision"):
        rows.extend(serialize(event) for event in service.list_events(limit=limit, event_type=event_type))
    rows.sort(key=lambda event: (str(event.get("created_at", "")), int(event.get("id", 0))), reverse=True)
    return {"ok": True, "rows": min(limit, len(rows)), "events": rows[:limit]}


def namespaces_payload(service: Any, *, limit: int) -> dict[str, Any]:
    claims = service.list_claims(limit=limit, include_archived=False)
    buckets: dict[str, list[Any]] = {"facts": [], "decisions": [], "workflows": [], "project_overview": []}
    for claim in claims:
        bucket = _claim_namespace(claim)
        buckets[bucket].append(claim)
        buckets["project_overview"].append(claim)
    return {
        "ok": True, "rows": len(claims),
        "namespaces": {name: {"count": len(rows), "samples": _namespace_samples(rows)} for name, rows in buckets.items()},
    }


def _claim_namespace(claim: Any) -> str:
    text = str(claim.text or "").lower()
    claim_type = str(claim.claim_type or "").lower()
    predicate = str(claim.predicate or "").lower()
    if "decision" in claim_type or predicate in {"decision", "policy"} or "decid" in text:
        return "decisions"
    if any(key in claim_type for key in {"workflow", "runbook", "process"}) or predicate in {"workflow", "runbook", "command", "step"}:
        return "workflows"
    return "facts"


def _namespace_samples(rows: list[Any]) -> list[dict[str, Any]]:
    return [
        {"id": int(item.id), "subject": item.subject, "predicate": item.predicate,
         "object_value": item.object_value, "status": item.status}
        for item in rows[:5]
    ]
