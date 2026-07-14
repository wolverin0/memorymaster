from __future__ import annotations

from pathlib import Path

from memorymaster.core.audit_envelope import build_audit_envelope
from memorymaster.core.service import MemoryService
from memorymaster.govern.operational_health import evaluate_operational_health, otel_status


def test_audit_envelope_is_attributable() -> None:
    envelope = build_audit_envelope(
        principal="operator-a",
        tenant_id="tenant-a",
        role="admin",
        request_id="request-a",
        session_id="session-a",
        action="recovery.drill",
        target="sqlite-backup",
        result="pass",
    )

    assert set(envelope) >= {
        "principal",
        "tenant_id",
        "role",
        "request_id",
        "session_id",
        "action",
        "target",
        "result",
        "occurred_at",
    }


def test_operational_health_persists_alert_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "ops.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()

    result = evaluate_operational_health(
        service,
        backup_manifest_dir=tmp_path / "missing-backups",
        persist=True,
        owner="platform-owner",
        runbook="docs/operations.md",
    )

    assert result["status"] == "alert"
    assert any(alert["code"] == "backup_missing" for alert in result["alerts"])
    events = service.list_events(limit=20, event_type="system")
    assert any(event.details == "operational_health_snapshot" for event in events)
    assert result["owner"] == "platform-owner"


def test_otel_is_explicitly_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MEMORYMASTER_OTEL_ENDPOINT", raising=False)
    assert otel_status()["status"] == "disabled"
