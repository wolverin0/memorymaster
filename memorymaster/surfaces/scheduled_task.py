"""Hidden Windows scheduled-task runner with durable local file logging.

Each run appends JSON lines to ``~/.memorymaster/logs/<mode>.log`` (T-0764):
``job_start``, the job's own records, and one ``job_finish`` with ``job``,
``scope``, ``duration_ms``, ``exit_code`` and ``outcome``. Output that a
steward hook script prints itself is still captured verbatim.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import runpy
from pathlib import Path

from memorymaster.core.structured_log import (
    configure_structured_logging,
    log_event,
    reset_structured_logging,
    timed_event,
)

_JOB_LOG = logging.getLogger("memorymaster.jobs")


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

        profile = run_compiled_profile(args.db, tenant_id=service.tenant_id)
    log_event(
        _JOB_LOG,
        "dream_result",
        surface="job",
        job="dream",
        queued=queued.to_dict(),
        capture=capture,
        dream=dream,
        compiled_profile=profile,
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
    try:
        runpy.run_path(args.script, run_name="__main__")
    except SystemExit as exc:
        # The steward cycle script ends with sys.exit(...): that is its exit code, not an
        # error (every run logged outcome=error, error_type=SystemExit until 2026-10-05).
        if exc.code is None or isinstance(exc.code, int):
            return int(exc.code or 0)
        raise
    return 0


def _log_path(mode: str) -> Path:
    directory = Path.home() / ".memorymaster" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{mode}.log"


def _job_scope(workspace: str) -> str | None:
    try:
        from memorymaster.core.scope_utils import scope_from_cwd

        return scope_from_cwd(workspace)
    except Exception:  # noqa: BLE001 - a log field must not fail the job
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with _log_path(args.mode).open("a", encoding="utf-8") as log:
        configure_structured_logging(log, component=f"job-{args.mode}")
        fields = {"surface": "job", "job": args.mode, "scope": _job_scope(args.workspace)}
        log_event(_JOB_LOG, "job_start", **fields)
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                with timed_event(_JOB_LOG, "job_finish", **fields) as result:
                    code = _run_dream(args) if args.mode == "dream" else _run_steward(args)
                    result["exit_code"] = code
                    if code:
                        result["outcome"] = "failed"
                    return code
        except Exception as exc:  # noqa: BLE001 - top-level task boundary (job_finish logged the error)
            log_event(_JOB_LOG, "job_error", level=logging.ERROR, **fields, error=str(exc)[:500])
            return 1
        finally:
            reset_structured_logging()


if __name__ == "__main__":
    raise SystemExit(main())
