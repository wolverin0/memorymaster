"""Persistent aggregate health snapshots and deterministic alerts."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any

from memorymaster.capture.coverage import capture_coverage
from memorymaster.core.audit_envelope import build_audit_envelope
from memorymaster.core.provider_health import (
    PROVIDER_FAILURE_EVENT_TYPE,
    configured_providers,
    parse_provider_failure,
)
from memorymaster.govern.recovery import backup_status


def otel_status() -> dict[str, str]:
    endpoint = os.environ.get("MEMORYMASTER_OTEL_ENDPOINT", "").strip()
    if not endpoint:
        return {"status": "disabled"}
    if importlib.util.find_spec("opentelemetry.sdk") is None:
        return {"status": "unavailable", "reason": "install the OpenTelemetry SDK"}
    return {"status": "configured"}


def error_tracking_status() -> dict[str, str]:
    endpoint = os.environ.get("MEMORYMASTER_ERROR_TRACKING_DSN", "").strip()
    if not endpoint:
        return {"status": "disabled"}
    if importlib.util.find_spec("sentry_sdk") is None:
        return {"status": "unavailable", "reason": "install the configured error-tracking SDK"}
    return {"status": "configured"}


def _retry_alerts(service: Any, threshold: int) -> tuple[dict[str, int], list[dict[str, Any]]]:
    counts = service.media_retry_status_counts()
    backlog = int(counts.get("pending", 0)) + int(counts.get("retrying", 0))
    alerts = []
    if backlog > threshold:
        alerts.append({"code": "media_backlog_high", "value": backlog, "threshold": threshold})
    return counts, alerts


def _database_signals(service: Any) -> dict[str, Any]:
    store = service.store
    with store.connect() as connection:
        connection.execute("SELECT 1")
        if hasattr(store, "db_path"):
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        else:
            integrity = "ok"
    db_path = Path(store.db_path) if hasattr(store, "db_path") else None
    wal_bytes = Path(f"{db_path}-wal").stat().st_size if db_path and Path(f"{db_path}-wal").exists() else 0
    disk = shutil.disk_usage(db_path.parent if db_path else Path.cwd())
    return {
        "integrity": integrity,
        "wal_bytes": wal_bytes,
        "disk_percent": round((disk.used / max(1, disk.total)) * 100, 2),
    }


PROVIDER_FAILURE_WINDOW_HOURS = 24


def _provider_failure_count(service: Any, *, window_hours: int = PROVIDER_FAILURE_WINDOW_HOURS) -> dict[str, Any]:
    """Count provider failures over a TIME window, and say what was examined.

    This used to scan ``list_events(limit=1000)`` with no filter. A row cap is
    not a time window: bookkeeping events dominate the log (487k
    `deterministic_adjust=+0.000` rows out of 2.4M in production), so those 1000
    rows spanned **13.9 minutes** -- an outage twenty minutes old was
    structurally invisible and the check reported healthy. That is the failure
    this module exists to catch, happening inside the check itself.

    Three changes make the signal honest:
      * bound by ``since`` so the window is the period a reader assumes;
      * return the window actually covered, so "0" can be distinguished from
        "the check saw almost nothing";
      * report, per CONFIGURED provider, whether the log has ever contained a
        failure from it (``producers``). R10 fixed the missing producer, but a
        producer only exists in a process that holds a writable store -- so a
        `0` still has to say whether it is evidence or an absence of evidence.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(window_hours)))).isoformat()

    # Producer evidence FIRST, so the broad windowed scan below is the last call
    # on `service.list_events` -- tests/test_inert_signals_r1_health_window.py
    # spies on that call to pin the time bound and the row cap.
    producers = _provider_producer_evidence(service)

    count = 0
    scanned = 0
    oldest: str | None = None
    by_provider: dict[str, int] = {}
    typed = 0
    for event in service.list_events(limit=100_000, since=since):
        scanned += 1
        created = str(getattr(event, "created_at", "") or "")
        if created and (oldest is None or created < oldest):
            oldest = created
        parsed = parse_provider_failure(event)
        if parsed is not None:
            typed += 1
            count += 1
            by_provider[parsed["provider"]] = by_provider.get(parsed["provider"], 0) + parsed["occurrences"]
            continue
        # Free-text fallback: rows written by other subsystems (and every row
        # predating R10) describe provider trouble in prose, not in a payload.
        detail = str(event.details or "").lower()
        if "provider" in detail and any(marker in detail for marker in ("fail", "error", "unavailable")):
            count += 1
            by_provider["unattributed"] = by_provider.get("unattributed", 0) + 1
    return {
        "count": count,
        "window_hours": int(window_hours),
        "events_scanned": scanned,
        "oldest_event_examined": oldest,
        "structured_events": typed,
        "by_provider": by_provider,
        "producers": producers,
    }


def _provider_producer_evidence(service: Any) -> dict[str, str]:
    """Per configured provider: has this log EVER carried a failure from it?

    ``observed`` -- a 0 in the window is a real all-clear for that provider.
    ``unproven`` -- nothing has ever recorded a failure for it here, so a 0 may
    simply mean no process with a writable store ever called it. This is the
    distinction R1's fix could not make and R10's producer makes possible;
    reporting it is what stops a fresh `provider_failures: 0` from being read as
    proof of health.

    Scans only ``system`` events (491 of 2.4M rows in production, and the type
    is indexed), so it is bounded by construction.
    """
    seen: set[str] = set()
    try:
        events = service.list_events(
            event_type=PROVIDER_FAILURE_EVENT_TYPE, limit=100_000, since=None,
        )
    except Exception:  # noqa: BLE001 - diagnostics must not break the health check
        return {name: "unknown" for name in configured_providers()}
    for event in events:
        parsed = parse_provider_failure(event)
        if parsed is not None:
            seen.add(parsed["provider"])
    return {
        name: ("observed" if name in seen else "unproven")
        for name in configured_providers()
    }


def evaluate_operational_health(
    service: Any,
    *,
    backup_manifest_dir: str | Path,
    persist: bool,
    owner: str,
    runbook: str,
    backup_max_age_hours: int = 24,
    retry_backlog_threshold: int = 100,
    wal_max_bytes: int = 512 * 1024 * 1024,
    disk_max_percent: float = 90.0,
    provider_failure_threshold: int = 0,
) -> dict[str, Any]:
    backup = backup_status(backup_manifest_dir, max_age_hours=backup_max_age_hours)
    retry_counts, alerts = _retry_alerts(service, retry_backlog_threshold)
    if backup["status"] != "ok":
        alerts.append({"code": backup["code"], "age_hours": backup.get("age_hours")})
    database = _database_signals(service)
    if database["integrity"] != "ok":
        alerts.append({"code": "database_integrity_failed"})
    if database["wal_bytes"] > max(0, int(wal_max_bytes)):
        alerts.append({"code": "wal_size_high", "value": database["wal_bytes"], "threshold": wal_max_bytes})
    if database["disk_percent"] > max(1.0, float(disk_max_percent)):
        alerts.append({"code": "disk_usage_high", "value": database["disk_percent"], "threshold": disk_max_percent})
    provider_health = _provider_failure_count(service)
    provider_failures = int(provider_health["count"])
    if provider_failures > max(0, int(provider_failure_threshold)):
        alerts.append({"code": "provider_failures", "value": provider_failures})
    capture = capture_coverage(service)
    if capture["status"] in {"attention", "broken"}:
        alerts.append({"code": f"capture_coverage_{capture['status']}"})
    result = {
        "schema_version": "memorymaster.operational-health.v1",
        "status": "alert" if alerts else "ok",
        "owner": owner,
        "runbook": runbook,
        "alerts": alerts,
        "backup": backup,
        "media_retry": retry_counts,
        "database": database,
        "provider_failures": provider_failures,
        # What the count actually covers -- a bare 0 hid a 14-minute window.
        "provider_failure_scan": provider_health,
        "capture": capture,
        "otel": otel_status(),
        "error_tracking": error_tracking_status(),
    }
    if persist:
        envelope = build_audit_envelope(
            principal=owner,
            tenant_id=getattr(service, "tenant_id", None),
            role="operator",
            request_id="scheduled-health",
            session_id="operational-health",
            action="operations.evaluate",
            target="memorymaster",
            result=result["status"],
        )
        service.store.record_event(
            claim_id=None,
            event_type="system",
            details="operational_health_snapshot",
            payload={"audit": envelope, "health": json.loads(json.dumps(result))},
        )
    return result
