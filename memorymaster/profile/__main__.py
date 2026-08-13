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
    if args.command == "status":
        result = _status(Path(args.db))
    else:
        from memorymaster.core.service import MemoryService

        MemoryService(args.db, workspace_root=Path(args.workspace)).init_db()
        result = run_compiled_profile(
            args.db,
            output_dir=args.output_dir or None,
            force=bool(args.force),
            max_map_calls=args.max_map_calls,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
