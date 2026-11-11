"""Hidden Windows scheduled-task runner with durable local file logging.

Each run appends JSON lines to ``~/.memorymaster/logs/<mode>.log`` (T-0764):
``job_start``, the job's own records, and one ``job_finish`` with ``job``,
``scope``, ``duration_ms``, ``exit_code`` and ``outcome``. Output that a
steward hook script prints itself is still captured verbatim.

``beat`` (AO-2 v1) is a short tick meant for a 5-minute schedule. It does nothing unless
``MEMORYMASTER_BEAT_ENABLED`` is truthy. See ``_run_beat``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import runpy
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.core.structured_log import (
    configure_structured_logging,
    log_event,
    reset_structured_logging,
    timed_event,
)

_JOB_LOG = logging.getLogger("memorymaster.jobs")

BEAT_ENABLED_ENV = "MEMORYMASTER_BEAT_ENABLED"
# One tick must end before the next 5-minute tick starts: the lease outlives a stuck run by at most this.
BEAT_LEASE_TTL_SECONDS = 270
# Slots equal consolidations: every picked capture is extracted first or already waiting. T-2123: 3 per tick
# left the backlog growing (252 consolidated in 37h against ~370 arrivals), so the defaults are 6. Spend stays
# bounded by the daily call/token caps, not by these; the clamp only bounds the ask.
# shortcut: a tick that outlasts the 5-minute cadence holds the lease, so the next tick skips its transcript scan;
# lower the env limits if beat.log shows worker_busy ticks, or move the scan out of the lease if that persists.
# BEAT_OLDEST_RESERVE slots stay oldest-first so the old backlog still drains; the rest go to freshly scanned sessions.
BEAT_MAX_SESSIONS = 6
BEAT_MAX_CONSOLIDATE_CAPTURES = 6
BEAT_MAX_SESSIONS_ENV = "MEMORYMASTER_BEAT_MAX_SESSIONS"
BEAT_MAX_CONSOLIDATE_CAPTURES_ENV = "MEMORYMASTER_BEAT_MAX_CONSOLIDATE_CAPTURES"
BEAT_MAX_PER_TICK_CLAMP = 30
BEAT_OLDEST_RESERVE = 1


def _beat_limit(env: str, default: int) -> int:
    """Per-tick limit from ``env``, clamped to 1..BEAT_MAX_PER_TICK_CLAMP (0 must never mean "uncapped")."""
    try:
        value = int(os.environ.get(env, str(default)))
    except ValueError:
        value = default
    return max(1, min(BEAT_MAX_PER_TICK_CLAMP, value))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memorymaster-scheduled-task")
    parser.add_argument("mode", choices=["dream", "steward", "beat", "verbatim-archive", "transcript-index"])
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


def _beat_enabled() -> bool:
    return os.environ.get(BEAT_ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _write_beat_artifact(workspace: str, state_db: Path, guard_log: Path) -> Path:
    """Persist M3/M4 as ``artifacts/<date>-ao2-beat-measurements.json`` (atomic replace)."""
    from memorymaster.operations.operational_review import compute_beat_measurements

    now = datetime.now(timezone.utc)
    payload = {"schema": "memorymaster.ao2-beat-measurements.v1",
               **compute_beat_measurements(state_db, guard_log, now=now)}
    directory = Path(workspace) / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{now.date().isoformat()}-ao2-beat-measurements.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def _run_beat(args: argparse.Namespace) -> int:
    """Run capture and the separately gated deterministic phase on every enabled tick."""
    if not _beat_enabled():
        return _run_capture_beat(args)
    outcomes: list[str] = []
    try:
        return _run_capture_beat(args, outcomes)
    finally:
        from memorymaster.profile.project_runtime import refresh_project_projections

        try:
            result = refresh_project_projections(skip=bool(outcomes and outcomes[0] == "skip"))
            if result.get("status") != "inactive":
                log_event(_JOB_LOG, "project_projection_tick", surface="job", job="beat", result=result)
        except Exception as exc:
            log_event(_JOB_LOG, "project_projection_error", surface="job", job="beat", error=type(exc).__name__)


def _run_capture_beat(args: argparse.Namespace, outcomes: list[str] | None = None) -> int:
    """One AO-2 beat tick: scan transcripts, ask the latency guard, then maybe run a bounded dream.

    Off unless MEMORYMASTER_BEAT_ENABLED is truthy. The guard is asked first; on ``skip`` nothing
    else is touched. Otherwise the tick holds the shared ``dream-worker`` lease while it scans, and
    releases it before ``run_dream``, which takes that same lease itself. Hourly Dreaming holding it is a normal no-op for the beat, not a failure.
    ``allow`` runs the worker with ``max_sessions`` and consolidated captures from ``BEAT_MAX_SESSIONS`` /
    ``BEAT_MAX_CONSOLIDATE_CAPTURES`` (6 each, env-configurable; one slot oldest-first), under the existing daily caps; ``capture_only`` and ``skip`` never call a model.
    Claims are applied only if the operator passes ``--apply-candidates``.
    """
    if not _beat_enabled():
        log_event(_JOB_LOG, "beat_tick", surface="job", job="beat", outcome="disabled")
        return 0
    from dataclasses import replace

    from memorymaster.core.capture_control import capture_state_path
    from memorymaster.dreaming import latency_guard
    from memorymaster.dreaming.capture import scan_active_transcripts
    from memorymaster.dreaming.ledger import DreamLedger
    from memorymaster.dreaming.worker import DreamConfig, run_dream

    state_db = capture_state_path()
    # The guard comes first: a hard breach (skip) must not scan directories, probe files or write
    # the state db, only record the decision.
    decision = latency_guard.evaluate()
    if outcomes is not None:
        outcomes.append(decision.action)
    scan: dict | None = None
    dream: dict | None = None
    if decision.action != latency_guard.SKIP:
        ledger = DreamLedger(state_db)
        owner = uuid.uuid4().hex
        if not ledger.acquire_lease("dream-worker", owner, BEAT_LEASE_TTL_SECONDS):
            log_event(_JOB_LOG, "beat_tick", surface="job", job="beat", outcome="worker_busy")
            return 0
        try:
            scan = scan_active_transcripts(ledger)
        finally:
            ledger.release_lease("dream-worker", owner)
        if decision.action == latency_guard.ALLOW:
            config = replace(
                DreamConfig.from_env(),
                lease_ttl_seconds=BEAT_LEASE_TTL_SECONDS,
                max_sessions=_beat_limit(BEAT_MAX_SESSIONS_ENV, BEAT_MAX_SESSIONS),
                max_consolidate_captures_per_run=_beat_limit(
                    BEAT_MAX_CONSOLIDATE_CAPTURES_ENV, BEAT_MAX_CONSOLIDATE_CAPTURES),
                oldest_reserve=BEAT_OLDEST_RESERVE,
            )
            dream = run_dream(args.db, args.workspace, apply_candidates=bool(args.apply_candidates), config=config)
    log_event(_JOB_LOG, "beat_tick", surface="job", job="beat", outcome=decision.action,
              reasons=decision.reasons, scan=scan, dream=dream)
    try:
        _write_beat_artifact(args.workspace, state_db, latency_guard.guard_log_path())
    except OSError as exc:
        log_event(_JOB_LOG, "beat_artifact_error", level=logging.WARNING, surface="job", job="beat",
                  error=type(exc).__name__)
    busy = bool(dream) and dream.get("reason") == "worker_busy"
    return 0 if dream is None or busy or (dream.get("ok") and not dream.get("errors")) else 1


def _run_verbatim_archive(args: argparse.Namespace) -> int:
    """Weekly retention (T-2116): move verbatim rows older than 90 days into the archive DB.

    A verify failure raises ArchiveVerifyError; main() logs it as job_error and exits 1."""
    from memorymaster.govern.verbatim_cleanup import archive_old

    result = archive_old(args.db, dry_run=False)
    log_event(_JOB_LOG, "verbatim_archive_result", surface="job", job="verbatim-archive", **result)
    return 0


# shortcut: fixed 30-day window keeps every run small and never widens the index to the full 28 GB
# history (T-1860 decision); raise it only if the fallback should answer questions older than a month.
TRANSCRIPT_INDEX_SINCE_DAYS = 30


def _run_transcript_index(args: argparse.Namespace) -> int:
    """Incremental refresh of the raw-transcript sidecar behind MEMORYMASTER_RECALL_TRANSCRIPT_FALLBACK (T-2116)."""
    from memorymaster.recall import transcript_index

    stats = transcript_index.index_transcripts(since_days=TRANSCRIPT_INDEX_SINCE_DAYS)
    failed = sorted(key[: -len("_failed")] for key, value in stats.items() if key.endswith("_failed") and value)
    log_event(_JOB_LOG, "transcript_index_result", surface="job", job="transcript-index",
              failed_sources=",".join(failed), **stats)
    # A failed RUN, not a skipped or unreadable session: a whole source could not be read (named in the log line),
    # or nothing at all could be.  Per-session errors alone stay exit 0.
    nothing_done = not (stats.get("files_indexed") or stats.get("files_skipped"))
    return 1 if failed or (stats.get("errors") and nothing_done) else 0


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
                    code = {"dream": _run_dream, "beat": _run_beat, "verbatim-archive": _run_verbatim_archive,
                                       "transcript-index": _run_transcript_index}.get(args.mode, _run_steward)(args)
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
