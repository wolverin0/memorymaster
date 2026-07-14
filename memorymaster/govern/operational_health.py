"""Persistent aggregate health snapshots and deterministic alerts."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
from typing import Any

from memorymaster.core.audit_envelope import build_audit_envelope
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


def _provider_failure_count(service: Any) -> int:
    count = 0
    for event in service.list_events(limit=1000):
        detail = str(event.details or "").lower()
        if "provider" in detail and any(marker in detail for marker in ("fail", "error", "unavailable")):
            count += 1
    return count


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
    provider_failures = _provider_failure_count(service)
    if provider_failures > max(0, int(provider_failure_threshold)):
        alerts.append({"code": "provider_failures", "value": provider_failures})
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
