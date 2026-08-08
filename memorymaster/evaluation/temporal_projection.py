"""Read-only temporal and episode projections for governed evaluation."""

from __future__ import annotations

import contextlib
import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from memorymaster.core.models import _parse_iso_strict
from memorymaster.core.security import is_sensitive_claim


REPORT_SCHEMA = "memorymaster.temporal-projection.v1"
_HISTORICAL_STATUSES = {"confirmed", "stale", "superseded"}
_INTENTS = {"current", "latest", "historical", "occurrence"}


def _query_timestamp(name: str, value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{name} is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _authorized(claim: Any, scopes: set[str], intent: str) -> bool:
    statuses = {"confirmed"} if intent in {"current", "latest"} else _HISTORICAL_STATUSES
    if claim.status not in statuses or claim.scope not in scopes:
        return False
    if getattr(claim, "visibility", "public") != "public":
        return False
    return not is_sensitive_claim(claim)


def _temporal_values(claim: Any) -> tuple[datetime | None, datetime | None, datetime | None]:
    return (
        _parse_iso_strict("event_time", claim.event_time),
        _parse_iso_strict("valid_from", claim.valid_from),
        _parse_iso_strict("valid_until", claim.valid_until),
    )


def _overlaps(
    start: datetime | None,
    end: datetime | None,
    query_start: datetime | None,
    query_end: datetime | None,
) -> bool:
    if query_start is None and query_end is None:
        return True
    left = start or datetime.min.replace(tzinfo=timezone.utc)
    right = end or datetime.max.replace(tzinfo=timezone.utc)
    query_left = query_start or datetime.min.replace(tzinfo=timezone.utc)
    query_right = query_end or datetime.max.replace(tzinfo=timezone.utc)
    return left <= query_right and right >= query_left


def _matches(
    claim: Any,
    values: tuple[datetime | None, datetime | None, datetime | None],
    intent: str,
    query_time: datetime | None,
    query_start: datetime | None,
    query_end: datetime | None,
) -> bool:
    event_time, valid_from, valid_until = values
    if intent in {"current", "latest"}:
        instant = query_time or datetime.now(timezone.utc)
        return claim.replaced_by_claim_id is None and _overlaps(
            valid_from, valid_until, instant, instant
        )
    if intent == "occurrence":
        return event_time is not None and _overlaps(
            event_time, event_time, query_start, query_end
        )
    if query_start is None and query_end is None:
        return True
    if valid_from is not None or valid_until is not None:
        return _overlaps(valid_from, valid_until, query_start, query_end)
    return event_time is not None and _overlaps(event_time, event_time, query_start, query_end)


def _record(claim: Any, values: tuple[datetime | None, datetime | None, datetime | None]) -> dict[str, Any]:
    event_time, valid_from, valid_until = values
    return {
        "claim_id": claim.id,
        "status": claim.status,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "replaced_by_claim_id": claim.replaced_by_claim_id,
        "occurrence_time": _iso(event_time),
        "capture_time": claim.created_at,
        "valid_from": _iso(valid_from),
        "valid_until": _iso(valid_until),
        "citations": [{"citation_id": citation.id} for citation in claim.citations],
    }


def _latest(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in records:
        key = (row["subject"], row["predicate"]) if row["subject"] or row["predicate"] else (row["claim_id"],)
        rank = row["occurrence_time"] or row["valid_from"] or row["capture_time"]
        existing = selected.get(key)
        existing_rank = "" if existing is None else (
            existing["occurrence_time"] or existing["valid_from"] or existing["capture_time"]
        )
        if existing is None or (rank, row["claim_id"]) > (existing_rank, existing["claim_id"]):
            selected[key] = row
    return list(selected.values())


def project_temporal_claims(
    service: Any,
    claim_ids: Sequence[int],
    *,
    scope_allowlist: Sequence[str],
    intent: str,
    query_time: str | None = None,
    query_start: str | None = None,
    query_end: str | None = None,
    max_claims: int = 50,
) -> dict[str, Any]:
    if intent not in _INTENTS:
        raise ValueError(f"unsupported temporal intent: {intent}")
    if not 1 <= max_claims <= 200:
        raise ValueError("max_claims must be between 1 and 200")
    instant = _query_timestamp("query_time", query_time)
    start = _query_timestamp("query_start", query_start)
    end = _query_timestamp("query_end", query_end)
    if start is not None and end is not None and end < start:
        raise ValueError("query_end is before query_start")
    diagnostics = {"unauthorized": 0, "temporal_filtered": 0, "malformed_temporal": 0}
    records: list[dict[str, Any]] = []
    scopes = {scope for scope in scope_allowlist if scope}
    for claim_id in list(dict.fromkeys(int(value) for value in claim_ids))[:max_claims]:
        claim = service.store.get_claim(claim_id)
        if claim is None or not _authorized(claim, scopes, intent):
            diagnostics["unauthorized"] += 1
            continue
        try:
            values = _temporal_values(claim)
        except ValueError:
            diagnostics["malformed_temporal"] += 1
            continue
        if not _matches(claim, values, intent, instant, start, end):
            diagnostics["temporal_filtered"] += 1
            continue
        records.append(_record(claim, values))
    if intent == "latest":
        records = _latest(records)
    records.sort(key=lambda row: (row["occurrence_time"] or row["valid_from"] or row["capture_time"], row["claim_id"]))
    return {
        "schema_version": REPORT_SCHEMA,
        "intent": intent,
        "claims": records,
        "diagnostics": diagnostics,
    }


def summarize_durative_states(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    omitted: list[int] = []
    for row in records:
        if not row.get("citations"):
            omitted.append(int(row["claim_id"]))
            continue
        key = (str(row.get("subject") or ""), str(row.get("predicate") or ""))
        grouped[key].append(row)
    states = []
    for (subject, predicate), rows in sorted(grouped.items()):
        starts = [row["valid_from"] for row in rows if row.get("valid_from")]
        ends = [row["valid_until"] for row in rows if row.get("valid_until")]
        states.append({
            "subject": subject,
            "predicate": predicate,
            "valid_from": min(starts) if starts else None,
            "valid_until": None if len(ends) != len(rows) else max(ends),
            "claim_ids": [row["claim_id"] for row in rows],
            "contributions": [
                {"claim_id": row["claim_id"], "citation_ids": [item["citation_id"] for item in row["citations"]]}
                for row in rows
            ],
        })
    return {"schema_version": REPORT_SCHEMA, "states": states, "omitted_uncited_claim_ids": omitted}


def _episode_rows(service: Any, claim_ids: list[int]) -> list[Any]:
    if not claim_ids:
        return []
    marks = ",".join("?" for _ in claim_ids)
    sql = f"""SELECT l.claim_id, e.id, s.id, s.source_id, s.chat_id, s.occurred_at, s.created_at
              FROM claim_evidence_links l
              JOIN evidence_items e ON e.id=l.evidence_item_id
              JOIN source_items s ON s.id=e.source_item_id
              WHERE l.claim_id IN ({marks}) AND s.retired_at IS NULL
                AND s.chat_id IS NOT NULL AND s.chat_id <> ''
                AND COALESCE(e.sensitivity, 'none') NOT IN ('high','redacted')
                AND COALESCE(s.sensitivity, 'none') NOT IN ('high','redacted')
              ORDER BY s.source_id, s.chat_id, COALESCE(s.occurred_at, s.created_at), s.id, e.id"""
    with contextlib.closing(service.store.connect()) as conn:
        return conn.execute(sql, claim_ids).fetchall()


def project_evidence_episodes(
    service: Any,
    claim_ids: Sequence[int],
    *,
    scope_allowlist: Sequence[str],
    max_window: int = 5,
    max_episodes: int = 20,
) -> dict[str, Any]:
    if not 1 <= max_window <= 20 or not 1 <= max_episodes <= 100:
        raise ValueError("episode bounds are outside the supported range")
    scopes = {scope for scope in scope_allowlist if scope}
    authorized = []
    for claim_id in dict.fromkeys(int(value) for value in claim_ids):
        claim = service.store.get_claim(claim_id)
        if claim is not None and _authorized(claim, scopes, "historical"):
            authorized.append(claim_id)
    grouped: dict[tuple[int, str], list[Any]] = defaultdict(list)
    for row in _episode_rows(service, authorized):
        grouped[(int(row[3]), str(row[4]))].append(row)
    episodes = []
    for (source_id, chat_id), rows in sorted(grouped.items())[:max_episodes]:
        window = rows[:max_window]
        source_items = {int(row[2]) for row in rows}
        episode_hash = hashlib.sha256(f"{source_id}:{chat_id}".encode()).hexdigest()[:16]
        episodes.append({
            "episode_id": episode_hash,
            "evidence_ids": [int(row[1]) for row in window],
            "source_item_ids": [int(row[2]) for row in window],
            "claim_ids": list(dict.fromkeys(int(row[0]) for row in window)),
            "recurring": len(source_items) > 1,
            "has_more": len(rows) > len(window),
        })
    return {"schema_version": REPORT_SCHEMA, "episodes": episodes}


__all__ = [
    "REPORT_SCHEMA",
    "project_evidence_episodes",
    "project_temporal_claims",
    "summarize_durative_states",
]
