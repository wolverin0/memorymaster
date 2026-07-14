"""Encrypted backup manifests and disposable recovery drills."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any

from memorymaster.stores import snapshot


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _manifest_path(backup_path: Path) -> Path:
    return backup_path.with_suffix(f"{backup_path.suffix}.manifest.json")


def _fernet(encryption_key: str):
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError(
            "encrypted recovery requires the 'security' extra: "
            "pip install 'memorymaster[security]'"
        ) from exc
    return Fernet(encryption_key.encode("ascii"))


def _decrypt(encryption_key: str, payload: bytes) -> bytes:
    try:
        from cryptography.fernet import InvalidToken
    except ImportError as exc:
        raise RuntimeError(
            "encrypted recovery requires the 'security' extra: "
            "pip install 'memorymaster[security]'"
        ) from exc
    try:
        return _fernet(encryption_key).decrypt(payload)
    except InvalidToken as exc:
        raise RuntimeError("encrypted backup authentication failed") from exc


def _sqlite_checks(db_path: Path) -> tuple[str, int]:
    connection = sqlite3.connect(str(db_path))
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        fk_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    return integrity, len(fk_rows)


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(f"{path.suffix}.part")
    partial.write_bytes(payload)
    partial.replace(path)


def create_encrypted_sqlite_backup(
    db_path: str | Path,
    destination: str | Path,
    *,
    encryption_key: str,
    off_device: bool,
    rpo_hours: int = 24,
    rto_minutes: int = 30,
) -> dict[str, Any]:
    source = Path(db_path).resolve()
    target = Path(destination).resolve()
    cipher = _fernet(encryption_key)
    with tempfile.TemporaryDirectory(prefix="memorymaster-backup-") as temporary:
        plaintext_path = Path(temporary) / "online-backup.db"
        snapshot.backup(source, plaintext_path)
        integrity, fk_violations = _sqlite_checks(plaintext_path)
        if integrity != "ok" or fk_violations:
            raise RuntimeError("online SQLite backup failed integrity validation")
        plaintext = plaintext_path.read_bytes()
    encrypted = cipher.encrypt(plaintext)
    _write_atomic(target, encrypted)
    manifest = {
        "schema_version": "memorymaster.recovery.v1",
        "backend": "sqlite",
        "created_at": _utc_now(),
        "encrypted": True,
        "off_device": bool(off_device),
        "rpo_hours": max(1, int(rpo_hours)),
        "rto_minutes": max(1, int(rto_minutes)),
        "plaintext_sha256": _sha256_bytes(plaintext),
        "ciphertext_sha256": _sha256_bytes(encrypted),
        "size_bytes": len(encrypted),
        "integrity_check": integrity,
        "foreign_key_violations": fk_violations,
    }
    _write_atomic(_manifest_path(target), (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
    return manifest


def run_sqlite_restore_drill(
    backup_path: str | Path,
    *,
    encryption_key: str,
) -> dict[str, Any]:
    source = Path(backup_path).resolve()
    manifest = json.loads(_manifest_path(source).read_text(encoding="utf-8"))
    encrypted = source.read_bytes()
    if _sha256_bytes(encrypted) != manifest["ciphertext_sha256"]:
        raise RuntimeError("encrypted backup checksum mismatch")
    started = time.monotonic()
    plaintext = _decrypt(encryption_key, encrypted)
    if _sha256_bytes(plaintext) != manifest["plaintext_sha256"]:
        raise RuntimeError("decrypted backup checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="memorymaster-restore-") as temporary:
        backup = Path(temporary) / "backup.db"
        restored = Path(temporary) / "restored.db"
        backup.write_bytes(plaintext)
        snapshot.restore(backup, restored)
        integrity, fk_violations = _sqlite_checks(restored)
    elapsed = time.monotonic() - started
    rto_seconds = int(manifest["rto_minutes"]) * 60
    return {
        "backend": "sqlite",
        "integrity_check": integrity,
        "foreign_key_violations": fk_violations,
        "elapsed_seconds": round(elapsed, 3),
        "rto_seconds": rto_seconds,
        "rto_met": integrity == "ok" and fk_violations == 0 and elapsed <= rto_seconds,
    }


def backup_status(manifest_dir: str | Path, *, max_age_hours: int) -> dict[str, Any]:
    manifests = sorted(Path(manifest_dir).glob("*.manifest.json"), key=lambda path: path.stat().st_mtime)
    if not manifests:
        return {"status": "alert", "code": "backup_missing", "age_hours": None}
    payload = json.loads(manifests[-1].read_text(encoding="utf-8"))
    created = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    return {
        "status": "ok" if age_hours <= max(1, int(max_age_hours)) else "alert",
        "code": "backup_fresh" if age_hours <= max(1, int(max_age_hours)) else "backup_stale",
        "age_hours": round(age_hours, 2),
    }


def postgres_recovery_plan() -> dict[str, Any]:
    return {
        "status": "BLOCKED-EXTERNAL",
        "backend": "postgres",
        "required_tools": ["pg_dump", "pg_restore"],
        "required_evidence": [
            "consistent custom-format dump",
            "restore into an empty disposable database",
            "schema and row-count verification",
            "restricted application-role smoke",
        ],
        "executes": False,
    }
