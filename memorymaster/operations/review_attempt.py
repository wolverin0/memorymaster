"""Atomic operational-review attempt evidence and fail-closed freshness reader."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def phase_progress(phase: str, duration_seconds: float | None = None) -> None:
    destination = os.environ.get("MEMORYMASTER_REVIEW_ATTEMPT_FILE")
    attempt_id = os.environ.get("MEMORYMASTER_REVIEW_ATTEMPT_ID")
    if not destination or not attempt_id:
        return
    path = Path(destination)
    attempt = read_json(path)
    if attempt.get("attempt_id") != attempt_id or attempt.get("completed_at"):
        return
    timings = dict(attempt.get("phase_seconds", {}))
    if duration_seconds is not None:
        timings[phase] = round(duration_seconds, 3)
    atomic_json(path, {**attempt, "phase": phase, "phase_seconds": timings,
                       "phase_updated_at": utc_now().isoformat()})


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed


def read_review_state(root: Path, *, now: datetime | None = None, interval_seconds: int | None = None) -> dict:
    """Never substitute a historical PASS for an unfinished or failed attempt."""
    current = now or utc_now()
    attempt = read_json(root / "attempt.json")
    latest = read_json(root / "latest.json")
    historical = {key: latest.get(key) for key in ("observed_at", "verdict", "attempt_id")}
    base = {"last_completed": historical, "attempt": attempt}
    if not attempt:
        return {**base, "verdict": "INCOMPLETE", "reason": "attempt_missing_or_invalid"}
    try:
        deadline = _time(attempt["deadline_at"])
        started = _time(attempt["started_at"])
        interval = interval_seconds if interval_seconds is not None else int(attempt["interval_seconds"])
        if interval <= 0 or started > deadline:
            raise ValueError("invalid interval")
        if not attempt.get("completed_at"):
            return {**base, "verdict": "TIMEOUT" if current >= deadline else "INCOMPLETE",
                    "reason": "deadline_expired" if current >= deadline else "attempt_running"}
        completed = _time(attempt["completed_at"])
        outcome = attempt.get("outcome")
        if outcome not in {"PASS", "WARN", "FAIL", "TIMEOUT", "INCOMPLETE"} or completed < started:
            raise ValueError("invalid completion")
        if outcome in {"PASS", "WARN", "FAIL"} and (
            latest.get("attempt_id") != attempt.get("attempt_id") or latest.get("verdict") != outcome
        ):
            return {**base, "verdict": "INCOMPLETE", "reason": "completed_artifact_mismatch"}
        stale = current > completed + timedelta(seconds=interval + 300)
        return {**base, "verdict": "STALE" if stale else outcome, "reason": "interval_expired" if stale else "attempt_finished",
                "fresh": not stale}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {**base, "verdict": "INCOMPLETE", "reason": "attempt_invalid"}
