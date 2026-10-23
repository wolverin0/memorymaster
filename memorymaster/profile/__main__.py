"""Command-line surface for the compiled user profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from memorymaster.profile.engine import run_compiled_profile
from memorymaster.profile.repository import ProfileRepository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m memorymaster.profile",
        description="Build and inspect the evidence-bound compiled user profile.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Show current compiled user profile state")
    status.add_argument("--db", required=True)
    run = commands.add_parser("run", help="Run or resume profile compilation")
    run.add_argument("--db", required=True)
    run.add_argument("--workspace", default=".")
    run.add_argument("--output-dir", default="")
    run.add_argument("--force", action="store_true")
    run.add_argument("--max-map-calls", type=int)
    for name in ("project-build", "project-read"):
        project = commands.add_parser(name, help="Explicitly build/read a governed project projection")
        project.add_argument("--db", required=True)
        project.add_argument("--scope", required=True)
        project.add_argument("--tenant", required=True, help="tenant key, or legacy-null for NULL only")
        project.add_argument("--context-key", required=True)
        project.add_argument("--epoch", required=True)
        project.add_argument("--project-root", required=True)
        project.add_argument("--output-dir", required=True)
        project.add_argument("--state-dir", required=True)
        project.add_argument("--ledger-dir")
        project.add_argument("--ledger-repo")
        project.add_argument("--ledger-filtered", action="store_true")
        project.add_argument("--alias", action="append", default=[])
    return parser


def _status(db_path: Path) -> dict[str, object]:
    if not db_path.is_file():
        return {"active_run": None, "facts": 0, "latest_completed_run": None}
    repository = ProfileRepository(db_path)
    active = repository.active_run()
    latest = repository.latest_completed_run()
    return {
        "active_run": int(active["id"]) if active else None,
        "facts": len(repository.active_facts()),
        "latest_completed_run": int(latest["id"]) if latest else None,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command.startswith("project-"):
        from memorymaster.profile.project_projection import (
            ProjectProjectionConfig,
            build_project_projection,
            publish_project_projection,
            read_project_projection,
        )
        from memorymaster.profile.project_runtime import PREFIX, active, resolve_projection_scope

        if not active(PREFIX + "ENABLED"):
            result = {"status": "inactive"}
        else:
            config = ProjectProjectionConfig(
                db_path=Path(args.db), scope=args.scope,
                tenant_id=None if args.tenant == "legacy-null" else args.tenant,
                context_key=args.context_key, epoch=args.epoch, project_root=Path(args.project_root),
                output_root=Path(args.output_dir), state_root=Path(args.state_dir),
                ledger_root=Path(args.ledger_dir) if args.ledger_dir else None,
                ledger_repo=args.ledger_repo, ledger_filtered=args.ledger_filtered,
                aliases=tuple(args.alias), enabled=True)
            if resolve_projection_scope(config, config.project_root) is None:
                result = {"ok": False, "status": "unauthorized_project"}
            elif args.command == "project-build":
                projection = build_project_projection(config)
                result = {"ok": publish_project_projection(config, projection),
                          "status": projection.status, "manifest": projection.public_manifest}
            else:
                projection = read_project_projection(config)
                result = ({"payload": projection.payload, "manifest": projection.public_manifest,
                           "status": projection.status} if projection else {"ok": False, "status": "unavailable"})
    elif args.command == "status":
        result = _status(Path(args.db))
    else:
        from memorymaster.core.service import MemoryService

        service = MemoryService(args.db, workspace_root=Path(args.workspace))
        service.init_db()
        result = run_compiled_profile(
            args.db,
            output_dir=args.output_dir or None,
            force=bool(args.force),
            max_map_calls=args.max_map_calls,
            tenant_id=service.tenant_id,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
