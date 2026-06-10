"""Confidence-prior calibration reports from validator event history."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SUCCESS_DETAILS = {"revalidation_passed"}
SUCCESS_STATUSES = {"confirmed"}
UNKNOWN_CLAIM_TYPE = "uncategorized"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _round_rate(successes: int, attempts: int) -> float:
    if attempts <= 0:
        return 0.0
    return round(successes / attempts, 4)


def _row_value(row: Any, key: str, index: int) -> Any:
    if isinstance(row, dict):
        return row[key]
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return row[index]


def _placeholder(store: Any) -> str:
    module = store.__class__.__module__
    return "%s" if module.endswith("postgres_store") else "?"


def compute_priors(store: Any, *, window_days: int = 90, now: datetime | None = None) -> dict[str, Any]:
    """Compute empirical validation-rate priors grouped by claim_type."""
    if window_days <= 0:
        raise ValueError("window_days must be positive")

    generated_at = (now or _utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    window_start = generated_at - timedelta(days=window_days)
    param = _placeholder(store)
    sql = f"""
        SELECT
            COALESCE(NULLIF(TRIM(c.claim_type), ''), '{UNKNOWN_CLAIM_TYPE}') AS claim_type,
            COUNT(*) AS attempts,
            SUM(
                CASE
                    WHEN e.to_status IN ('confirmed')
                         OR e.details IN ('revalidation_passed')
                    THEN 1
                    ELSE 0
                END
            ) AS validated
        FROM events e
        JOIN claims c ON c.id = e.claim_id
        WHERE e.event_type = 'validator'
          AND e.claim_id IS NOT NULL
          AND e.created_at >= {param}
        GROUP BY COALESCE(NULLIF(TRIM(c.claim_type), ''), '{UNKNOWN_CLAIM_TYPE}')
        ORDER BY claim_type
    """

    with store.connect() as conn:
        rows = conn.execute(sql, (window_start.isoformat(),)).fetchall()

    priors = []
    total_attempts = 0
    total_validated = 0
    for row in rows:
        attempts = int(_row_value(row, "attempts", 1) or 0)
        validated = int(_row_value(row, "validated", 2) or 0)
        rate = _round_rate(validated, attempts)
        total_attempts += attempts
        total_validated += validated
        priors.append(
            {
                "claim_type": str(_row_value(row, "claim_type", 0)),
                "validation_attempts": attempts,
                "validated": validated,
                "empirical_validation_rate": rate,
                "recommended_initial_confidence": rate,
            }
        )

    global_rate = _round_rate(total_validated, total_attempts)
    return {
        "generated_at": generated_at.isoformat(),
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "window_end": generated_at.isoformat(),
        "source": "events",
        "event_type": "validator",
        "success_statuses": sorted(SUCCESS_STATUSES),
        "success_details": sorted(SUCCESS_DETAILS),
        "total_attempts": total_attempts,
        "total_validated": total_validated,
        "global_empirical_validation_rate": global_rate,
        "default_recommended_initial_confidence": global_rate if total_attempts else 0.5,
        "priors": priors,
    }


def write_report(report: dict[str, Any], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def run(store: Any, *, window_days: int = 90, output: str | Path) -> dict[str, Any]:
    """Compute and write a confidence-prior report without mutating claims/config."""
    report = compute_priors(store, window_days=window_days)
    output_path = write_report(report, output)
    return {**report, "output": str(output_path)}
