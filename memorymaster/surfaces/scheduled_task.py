"""Hidden Windows scheduled-task runner with durable local file logging."""

from __future__ import annotations

import argparse
import contextlib
import os
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
    parser.add_argument("--extract-provider", default="")
    parser.add_argument("--extract-model", default="")
    parser.add_argument("--extract-variant", default="")
    parser.add_argument("--consolidate-model", default="")
    parser.add_argument("--consolidate-variant", default="")
    parser.add_argument("--clear-provider-variants", action="store_true")
    return parser


def _apply_dream_provider_contract(args: argparse.Namespace) -> None:
    values = {
        "MEMORYMASTER_DREAM_EXTRACT_PROVIDER": getattr(args, "extract_provider", ""),
        "MEMORYMASTER_DREAM_EXTRACT_MODEL": getattr(args, "extract_model", ""),
        "MEMORYMASTER_DREAM_CONSOLIDATE_MODEL": getattr(args, "consolidate_model", ""),
    }
    if getattr(args, "clear_provider_variants", False):
        os.environ.pop("MEMORYMASTER_DREAM_EXTRACT_VARIANT", None)
        os.environ.pop("MEMORYMASTER_DREAM_CONSOLIDATE_VARIANT", None)
    values.update({
        "MEMORYMASTER_DREAM_EXTRACT_VARIANT": getattr(args, "extract_variant", ""),
        "MEMORYMASTER_DREAM_CONSOLIDATE_VARIANT": getattr(args, "consolidate_variant", ""),
    })
    for name, value in values.items():
        if value:
            os.environ[name] = value


def _capture_error_count(capture: object) -> int:
    if isinstance(capture, dict):
        return int(capture.get("errors", 0) or 0)
    return int(getattr(capture, "errors", 0) or 0)


def _compiled_profile_enabled() -> bool:
    return os.environ.get("MEMORYMASTER_COMPILED_PROFILE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _run_dream(args: argparse.Namespace) -> int:
    _apply_dream_provider_contract(args)
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
    profile = {"ok": True, "status": "disabled"}
    if _compiled_profile_enabled():
        from memorymaster.profile.engine import run_compiled_profile

        profile = run_compiled_profile(args.db)
    print(
        {
            "queued": queued.to_dict(),
            "capture": capture,
            "dream": dream,
            "compiled_profile": profile,
        }
    )
    passed = (
        dream.get("ok")
        and not dream.get("errors")
        and not _capture_error_count(capture)
        and profile.get("ok")
    )
    return 0 if passed else 1


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
