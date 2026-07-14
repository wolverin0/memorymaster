"""Fail-closed operational CLI for recovery, health, and privacy planning."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from memorymaster.core.service import MemoryService
from memorymaster.govern.operational_health import evaluate_operational_health
from memorymaster.govern.privacy_ops import PrivacySelector, build_privacy_plan
from memorymaster.govern.recovery import (
    create_encrypted_sqlite_backup,
    postgres_recovery_plan,
    run_sqlite_restore_drill,
)


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def _required_backup_key() -> str:
    key = os.environ.get("MEMORYMASTER_BACKUP_KEY", "").strip()
    if not key:
        raise RuntimeError("MEMORYMASTER_BACKUP_KEY is required; keys are never accepted as CLI arguments")
    return key


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MemoryMaster recovery, health, and privacy operations")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup-create")
    backup.add_argument("--db", required=True)
    backup.add_argument("--destination", required=True)
    backup.add_argument("--off-device", action="store_true")
    backup.add_argument("--rpo-hours", type=int, default=24)
    backup.add_argument("--rto-minutes", type=int, default=30)
    drill = commands.add_parser("restore-drill")
    drill.add_argument("--backup", required=True)
    health = commands.add_parser("health")
    health.add_argument("--db", required=True)
    health.add_argument("--workspace", default=".")
    health.add_argument("--manifest-dir", required=True)
    health.add_argument("--owner", required=True)
    health.add_argument("--runbook", default="docs/operations.md")
    health.add_argument("--persist", action="store_true")
    privacy = commands.add_parser("privacy-plan")
    privacy.add_argument("--db", required=True)
    privacy.add_argument("--workspace", default=".")
    privacy.add_argument("--principal", required=True)
    privacy.add_argument("--tenant-id")
    privacy.add_argument("--scope")
    commands.add_parser("postgres-recovery-plan")
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "backup-create":
        return create_encrypted_sqlite_backup(
            args.db,
            args.destination,
            encryption_key=_required_backup_key(),
            off_device=args.off_device,
            rpo_hours=args.rpo_hours,
            rto_minutes=args.rto_minutes,
        )
    if args.command == "restore-drill":
        return run_sqlite_restore_drill(args.backup, encryption_key=_required_backup_key())
    if args.command == "postgres-recovery-plan":
        return postgres_recovery_plan()
    if args.command == "privacy-plan":
        return build_privacy_plan(
            db_target=args.db,
            workspace=args.workspace,
            selector=PrivacySelector(args.principal, args.tenant_id, args.scope),
        )
    service = MemoryService(args.db, workspace_root=Path(args.workspace))
    return evaluate_operational_health(
        service,
        backup_manifest_dir=args.manifest_dir,
        persist=args.persist,
        owner=args.owner,
        runbook=args.runbook,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        _print(_run(_build_parser().parse_args(argv)))
    except (OSError, RuntimeError, ValueError) as exc:
        _print({"ok": False, "error": str(exc)})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
