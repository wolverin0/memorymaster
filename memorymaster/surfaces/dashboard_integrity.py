"""Reliability panel data for the dashboard (P1 WAL-discipline spec §2.10).

Builds the ``/api/integrity`` JSON: live WAL size / spool depth / freeze
state plus the latest persisted ``integrity_metrics``, ``qdrant_drift`` and
``spool_drain`` system events (written by ``jobs/integrity.emit_metrics`` and
friends each steward cycle). These fields are the §5 flip criteria and the
§7 escalation-tripwire inputs — the dashboard is where the operator checks
them at day 7 before flipping the flag default ON.

Lives outside ``dashboard.py`` (already >800 LOC) so the panel logic stays
testable without an HTTP server.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from memorymaster._storage_shared import busy_error_count, connect_ro
from memorymaster.jobs.integrity import (
    MARKER_METRICS,
    _wal_bytes,
    promotions_frozen,
)
from memorymaster.jobs.qdrant_reconcile import MARKER_DRIFT
from memorymaster.jobs.spool_drain import MARKER_DRAIN
from memorymaster.spool import pending_depth


def _latest_marker_payload(
    conn: sqlite3.Connection, details: str
) -> dict[str, Any] | None:
    """Payload + timestamp of the newest `system` event with this marker."""
    row = conn.execute(
        "SELECT payload_json, created_at FROM events"
        " WHERE event_type = 'system' AND details = ?"
        " ORDER BY id DESC LIMIT 1",
        (details,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0]) if row[0] else {}
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {"raw": payload}
    payload["at"] = row[1]
    return payload


def build_integrity_panel(service: Any) -> dict[str, Any]:
    """§2.10 panel data: WAL, spool depth/drain, qdrant drift, busy errors."""
    store = getattr(service, "store", None)
    db_path = getattr(store, "db_path", None)
    if not db_path or not Path(db_path).exists():
        return {"available": False, "reason": "no_sqlite_db"}

    depth = pending_depth(db_path)
    panel: dict[str, Any] = {
        "available": True,
        "db": str(db_path),
        "wal_bytes": _wal_bytes(db_path),
        "promotions_frozen": promotions_frozen(db_path),
        "busy_errors": busy_error_count(),
        "spool": {
            "depth_files": depth["files"],
            "depth_lines": depth["lines"],
            "last_drain": None,
        },
        "qdrant": {"drift": None},
        "last_cycle": None,
    }
    try:
        with closing(connect_ro(db_path)) as conn:
            panel["last_cycle"] = _latest_marker_payload(conn, MARKER_METRICS)
            panel["spool"]["last_drain"] = _latest_marker_payload(conn, MARKER_DRAIN)
            drift = _latest_marker_payload(conn, MARKER_DRIFT)
            if drift is not None:
                panel["qdrant"] = drift
    except sqlite3.Error as exc:
        panel["events_error"] = str(exc)
    return panel
