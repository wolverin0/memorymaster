"""Read-only, fourteen-day Antigravity consolidation latency report (JSON)."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3


def measure(database: Path, *, now: datetime | None = None) -> dict:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=14)
    # mode=ro refuses missing files and prevents database writes; do not use the
    # DreamLedger constructor, which initializes/migrates its database.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro")) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        calls = conn.execute(
            """SELECT u.latency_ms, u.outcome, u.http_status
               FROM dream_provider_usage u JOIN dream_runs r USING (run_id)
               WHERE u.provider = 'antigravity' AND u.model = r.consolidator_model
                 AND julianday(u.created_at) >= julianday(?)""",
            (cutoff.isoformat(),),
        ).fetchall()
        summaries = conn.execute(
            "SELECT summary_json FROM dream_runs WHERE julianday(started_at) >= julianday(?)",
            (cutoff.isoformat(),),
        ).fetchall()
    # Errors currently have latency_ms=0: they are unknown, not instant calls.
    durations = sorted(row["latency_ms"] / 1000 for row in calls if row["latency_ms"] > 0)
    breakers = 0
    invalid_summaries = 0
    for row in summaries:
        try:
            summary = json.loads(row["summary_json"] or "{}")
            if not isinstance(summary, dict):
                raise ValueError("summary is not an object")
        except (ValueError, TypeError):
            invalid_summaries += 1
            continue
        breakers += summary.get("consolidate_breaker") == "TimeoutExpired"
    return {
        "window_days": 14,
        "since_utc": cutoff.isoformat(),
        "duration_source": "dream_provider_usage.latency_ms (positive values; nearest-rank percentiles)",
        "stage_filter": "provider=antigravity and model=dream_runs.consolidator_model (no stage column)",
        "calls": len(calls),
        "measured_calls": len(durations),
        "unknown_duration_calls": len(calls) - len(durations),
        "p50_seconds": durations[math.ceil(len(durations) * 0.50) - 1] if durations else None,
        "p95_seconds": durations[math.ceil(len(durations) * 0.95) - 1] if durations else None,
        "max_seconds": max(durations) if durations else None,
        "outcomes": dict(Counter(row["outcome"] for row in calls)),
        "http_statuses": dict(Counter(str(row["http_status"]) for row in calls)),
        "runs_with_consolidate_breaker_TimeoutExpired": breakers,
        "invalid_run_summaries": invalid_summaries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Path to capture-control database (opened mode=ro)")
    args = parser.parse_args()
    print(json.dumps(measure(args.database), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
