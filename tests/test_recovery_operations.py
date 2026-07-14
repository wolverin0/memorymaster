from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from cryptography.fernet import Fernet

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.recovery import (
    backup_status,
    create_encrypted_sqlite_backup,
    postgres_recovery_plan,
    run_sqlite_restore_drill,
)


def _database(tmp_path: Path) -> Path:
    db = tmp_path / "source" / "memory.db"
    db.parent.mkdir(parents=True)
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    service.ingest(
        "Recovery drill preserves this claim.",
        citations=[CitationInput(source="test", locator="recovery")],
        source_agent="recovery-test",
    )
    return db


def test_encrypted_off_device_backup_roundtrip_and_integrity(tmp_path: Path) -> None:
    db = _database(tmp_path)
    key = Fernet.generate_key().decode()
    destination = tmp_path / "off-device" / "memory.db.enc"

    manifest = create_encrypted_sqlite_backup(
        db,
        destination,
        encryption_key=key,
        off_device=True,
        rpo_hours=24,
        rto_minutes=15,
    )

    assert destination.exists()
    assert not destination.with_suffix(".plain").exists()
    assert manifest["encrypted"] is True
    assert manifest["off_device"] is True
    assert len(manifest["plaintext_sha256"]) == 64
    assert json.loads(destination.with_suffix(".enc.manifest.json").read_text())["backend"] == "sqlite"

    drill = run_sqlite_restore_drill(destination, encryption_key=key)
    assert drill["integrity_check"] == "ok"
    assert drill["foreign_key_violations"] == 0
    assert drill["rto_met"] is True


def test_backup_age_alerts_on_stale_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "stale.manifest.json"
    manifest.write_text(
        json.dumps({"created_at": (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()}),
        encoding="utf-8",
    )

    status = backup_status(tmp_path, max_age_hours=24)
    assert status["status"] == "alert"
    assert status["age_hours"] >= 48


def test_postgres_recovery_is_a_redacted_external_plan() -> None:
    plan = postgres_recovery_plan()

    assert plan["status"] == "BLOCKED-EXTERNAL"
    assert plan["backend"] == "postgres"
    assert "pg_dump" in plan["required_tools"]
    assert "dsn" not in json.dumps(plan).lower()
