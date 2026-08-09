"""Hidden Windows scheduled-task runner with durable local file logging."""

from __future__ import annotations

import argparse
import contextlib
import runpy
from datetime import datetime, timezone
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memorymaster-scheduled-task")
    parser.add_argument("mode", choices=["dream", "steward"])
    parser.add_argument("--db", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--script", default="")
    parser.add_argument("--apply-candidates", action="store_true")
    return parser


def _run_dream(args: argparse.Namespace) -> int:
    from memorymaster.capture.worker import run_capture_worker
    from memorymaster.core.service import MemoryService
    from memorymaster.dreaming.worker import run_dream
    from memorymaster.public.v1 import improve

    service = MemoryService(args.db, workspace_root=Path(args.workspace))
    service.init_db()
    queued = improve(
        db=args.db,
        workspace=args.workspace,
        max_items=25,
        source_agent="memorymaster-dreaming",
        platform="scheduled",
    )
    capture = run_capture_worker(service, limit=25)
    dream = run_dream(
        args.db,
        args.workspace,
        apply_candidates=bool(args.apply_candidates),
    )
    print({"queued": queued.to_dict(), "capture": capture, "dream": dream})
    return 0 if dream.get("ok") and not dream.get("errors") else 1


def _run_steward(args: argparse.Namespace) -> int:
    if not args.script:
        raise ValueError("--script is required for steward mode")
    runpy.run_path(args.script, run_name="__main__")
    return 0


def _log_path(mode: str) -> Path:
    directory = Path.home() / ".memorymaster" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{mode}.log"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with _log_path(args.mode).open("a", encoding="utf-8") as log:
        timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        print(f"\n[{timestamp}] {args.mode} start", file=log)
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                return _run_dream(args) if args.mode == "dream" else _run_steward(args)
        except Exception as exc:  # noqa: BLE001 - top-level task boundary
            print(f"scheduled task failed: {exc}", file=log)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
