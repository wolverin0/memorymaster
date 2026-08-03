"""Content-free capture completeness read model for operator surfaces."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memorymaster.capture.models import CAPTURE_JOB_STATUSES, CAPTURE_STAGES
from memorymaster.capture.repository import graph_job_content_hash
from memorymaster.stores._storage_shared import connect_ro

COVERAGE_SCHEMA_VERSION = "memorymaster.capture-coverage.v1"
_REQUIRED_TABLES = frozenset(
    {"source_items", "evidence_items", "claims", "claim_evidence_links", "capture_jobs"}
)


def _mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _payload_scope(raw: Any) -> str | None:
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            return None
    return str(payload.get("scope")) if isinstance(payload, dict) and payload.get("scope") else None


def _in_scope(raw: Any, scope: str | None) -> bool:
    return scope is None or _payload_scope(raw) == scope


def _is_due(value: Any, now: datetime) -> bool:
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= now


def _schema_ready(conn: Any) -> bool:
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except (AttributeError, sqlite3.Error):
        return False
    return _REQUIRED_TABLES <= {str(_mapping(row)["name"]) for row in rows}


def _source_and_evidence(conn: Any, scope: str | None) -> tuple[int, int, list[int]]:
    sources = conn.execute(
        "SELECT id, payload_json FROM source_items WHERE retired_at IS NULL ORDER BY id"
    ).fetchall()
    active_sources = sum(_in_scope(row["payload_json"], scope) for row in sources)
    evidence = conn.execute(
        """SELECT e.id, e.text, e.content_hash, s.content_hash AS source_hash,
                  s.id AS source_item_id, s.payload_json,
                  EXISTS (
                      SELECT 1 FROM capture_jobs j
                      WHERE j.source_item_id=s.id
                        AND j.content_hash=COALESCE(e.content_hash, s.content_hash)
                        AND j.stage='extract_claims'
                  ) AS has_claim_job
           FROM evidence_items e JOIN source_items s ON s.id=e.source_item_id
           WHERE s.retired_at IS NULL ORDER BY e.id"""
    ).fetchall()
    scoped = [row for row in evidence if _in_scope(row["payload_json"], scope)]
    missing = [
        int(row["id"])
        for row in scoped
        if row["text"] is not None
        and (row["content_hash"] or row["source_hash"])
        and not bool(row["has_claim_job"])
    ]
    return int(active_sources), len(scoped), missing


def _graph_coverage(conn: Any, scope: str | None) -> tuple[int, list[int]]:
    rows = conn.execute(
        """SELECT c.id AS claim_id, c.updated_at, c.scope
           FROM claims c
           JOIN claim_evidence_links cel ON cel.claim_id=c.id
           JOIN evidence_items e ON e.id=cel.evidence_item_id
           JOIN source_items s ON s.id=e.source_item_id
           WHERE c.status='confirmed' AND s.retired_at IS NULL
           GROUP BY c.id, c.updated_at, c.scope ORDER BY c.id"""
    ).fetchall()
    scoped = [row for row in rows if scope is None or str(row["scope"]) == scope]
    hashes = {
        str(row["content_hash"])
        for row in conn.execute(
            "SELECT content_hash FROM capture_jobs WHERE stage='extract_graph'"
        ).fetchall()
    }
    missing = [
        int(row["claim_id"])
        for row in scoped
        if graph_job_content_hash(row["claim_id"], row["updated_at"]) not in hashes
    ]
    return len(scoped), missing


def _job_coverage(conn: Any, scope: str | None, now: datetime) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT j.id, j.stage, j.status, j.next_attempt_at, j.lease_expires_at,
                  j.error_code, s.payload_json
           FROM capture_jobs j JOIN source_items s ON s.id=j.source_item_id
           ORDER BY j.id"""
    ).fetchall()
    scoped = [_mapping(row) for row in rows if _in_scope(row["payload_json"], scope)]
    counts = {stage: {status: 0 for status in CAPTURE_JOB_STATUSES} for stage in CAPTURE_STAGES}
    for row in scoped:
        counts[str(row["stage"])][str(row["status"])] += 1
    blocked = [row for row in scoped if row["status"] == "blocked"]
    expired = [
        row for row in scoped
        if row["status"] == "leased" and _is_due(row["lease_expires_at"], now)
    ]
    due_retryable = [
        row for row in scoped
        if row["status"] == "retryable" and _is_due(row["next_attempt_at"], now)
    ]
    partial = [
        row for row in scoped
        if row["status"] == "completed" and row["error_code"] == "partial_provider_output"
    ]
    return {
        "counts": counts,
        "blocked": blocked,
        "expired": expired,
        "due_retryable": due_retryable,
        "partial": partial,
    }


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "status": "unavailable",
        "coverage_complete": False,
        "reason": reason,
    }


def _report(conn: Any, *, scope: str | None, now: datetime) -> dict[str, Any]:
    sources, evidence, missing_claims = _source_and_evidence(conn, scope)
    confirmed, missing_graph = _graph_coverage(conn, scope)
    jobs = _job_coverage(conn, scope, now)
    orphan_rows = conn.execute(
        """SELECT j.id FROM capture_jobs j LEFT JOIN source_items s
           ON s.id=j.source_item_id WHERE s.id IS NULL ORDER BY j.id"""
    ).fetchall()
    orphan_ids = [int(row["id"]) for row in orphan_rows]
    broken = bool(missing_claims or missing_graph or jobs["expired"] or orphan_ids)
    attention = bool(jobs["blocked"] or jobs["due_retryable"] or jobs["partial"])
    anomalies = _anomalies(missing_claims, missing_graph, orphan_ids, jobs)
    return {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "status": "broken" if broken else "attention" if attention else "ok",
        "scope": scope or "*",
        "coverage_complete": not broken,
        "counts": {
            "active_sources": sources,
            "active_evidence": evidence,
            "confirmed_claims": confirmed,
        },
        "jobs": jobs["counts"],
        "anomalies": anomalies,
    }


def _anomalies(
    missing_claims: list[int],
    missing_graph: list[int],
    orphan_ids: list[int],
    jobs: dict[str, Any],
) -> dict[str, Any]:
    blocked_codes = Counter(str(row["error_code"] or "unknown") for row in jobs["blocked"])
    return {
        "missing_claim_jobs": {"count": len(missing_claims), "evidence_ids": missing_claims[:100]},
        "missing_graph_jobs": {"count": len(missing_graph), "claim_ids": missing_graph[:100]},
        "expired_leases": {"count": len(jobs["expired"]), "job_ids": [int(row["id"]) for row in jobs["expired"][:100]]},
        "due_retryable": {"count": len(jobs["due_retryable"]), "job_ids": [int(row["id"]) for row in jobs["due_retryable"][:100]]},
        "blocked": {"count": len(jobs["blocked"]), "codes": dict(sorted(blocked_codes.items()))},
        "partial_completed": {"count": len(jobs["partial"]), "job_ids": [int(row["id"]) for row in jobs["partial"][:100]]},
        "orphan_jobs": {"count": len(orphan_ids), "job_ids": orphan_ids[:100]},
    }


def capture_coverage(
    service: Any, *, scope: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Return scope-aware capture completeness without source or claim text."""
    try:
        with service.store.connect() as conn:
            if not _schema_ready(conn):
                return _unavailable("schema_unavailable")
            return _report(conn, scope=scope, now=now or datetime.now(timezone.utc))
    except sqlite3.Error:
        return _unavailable("read_failed")


def capture_coverage_from_path(
    db_path: str | Path, *, scope: str | None = None
) -> dict[str, Any]:
    """Inspect an existing SQLite database read-only without running migrations."""
    path = Path(db_path)
    if not path.is_file():
        return _unavailable("database_missing")
    try:
        with connect_ro(path.resolve()) as conn:
            if not _schema_ready(conn):
                return _unavailable("schema_unavailable")
            return _report(conn, scope=scope, now=datetime.now(timezone.utc))
    except sqlite3.Error:
        return _unavailable("read_failed")
