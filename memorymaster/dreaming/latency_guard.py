"""Latency guard for the AO-2 beat: decide whether a tick may do model work.

The beat shares the machine with the interactive recall hook. Before each tick it
reads two recent signals and returns ``allow``, ``capture_only`` or ``skip``:

* dense recall events (``recall-dense.jsonl``), thresholds soft 150 ms / hard 300 ms;
* end-to-end hook durations (``hook.log`` ``recall / latency_total``), thresholds
  soft 900 ms / hard 1200 ms, the T-1042 bound.

A signal with fewer than ``MEMORYMASTER_BEAT_GUARD_MIN_EVENTS`` events in the window
(missing log, cold start, quiet hour) says nothing and never blocks. An error inside the
guard is also an ``allow``: the guard may only ever be the reason the beat does less
because it saw a slow hook, never because it could not look.

Every decision is appended as one JSON line to ``beat-guard.jsonl`` in the state dir.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ALLOW = "allow"
CAPTURE_ONLY = "capture_only"
SKIP = "skip"

DENSE_SOFT_ENV = "MEMORYMASTER_BEAT_DENSE_SOFT_MS"
DENSE_HARD_ENV = "MEMORYMASTER_BEAT_DENSE_HARD_MS"
HOOK_SOFT_ENV = "MEMORYMASTER_BEAT_HOOK_SOFT_MS"
HOOK_HARD_ENV = "MEMORYMASTER_BEAT_HOOK_HARD_MS"
MIN_EVENTS_ENV = "MEMORYMASTER_BEAT_GUARD_MIN_EVENTS"
WINDOW_ENV = "MEMORYMASTER_BEAT_GUARD_WINDOW_MINUTES"

DEFAULT_DENSE_SOFT_MS = 150.0
DEFAULT_DENSE_HARD_MS = 300.0
DEFAULT_HOOK_SOFT_MS = 900.0
DEFAULT_HOOK_HARD_MS = 1200.0
DEFAULT_MIN_EVENTS = 5
DEFAULT_WINDOW_MINUTES = 60

HOOK_LOG_TAIL_BYTES = 512 * 1024
GUARD_LOG_MAX_BYTES = 1024 * 1024
GUARD_LOG_NAME = "beat-guard.jsonl"

_HOOK_LINE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+hook=recall\b.*\bevent=(?P<event>latency_total|timing)\b.*\btotal_ms=(?P<ms>[0-9.]+)")


@dataclass(frozen=True, slots=True)
class GuardDecision:
    action: str
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def _float_env(name: str, default: float) -> float:
    """A finite float from the environment; anything else (garbage, nan, inf) is the default."""
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def _parse_ts(value: str) -> datetime:
    """ISO timestamp to an aware datetime; a trailing Z is read as UTC (Python 3.10 rejects it)."""
    stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    return stamp if stamp.tzinfo else stamp.astimezone()


def guard_log_path() -> Path:
    from memorymaster.core.capture_control import capture_state_path

    return capture_state_path().parent / GUARD_LOG_NAME


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    data = sorted(values)
    return round(data[min(len(data) - 1, int(0.95 * len(data)))], 1)


def _dense_metrics(path: Path, since: datetime) -> dict[str, Any]:
    from memorymaster.recall import dense_recall

    totals = [float(event.get("total_ms") or 0) for event in dense_recall.read_events(path, since=since)]
    return {"n": len(totals), "p95_ms": _p95(totals)}


def _hook_metrics(path: Path, since: datetime) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - HOOK_LOG_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return {"n": 0, "p95_ms": None}
    totals: list[float] = []
    seen: dict[tuple[str, str], dict[str, int]] = {}
    for raw in text.splitlines():
        match = _HOOK_LINE.match(raw)
        if match is None:
            continue
        try:
            stamp = _parse_ts(match.group("ts"))
        except ValueError:
            continue
        identity = (match.group("ts"), match.group("ms"))
        if stamp >= since:
            counts = seen.setdefault(identity, {"timing": 0, "latency_total": 0})
            event = match.group("event")
            other = "timing" if event == "latency_total" else "latency_total"
            counts[event] += 1
            # Pair mirrored formats, preserving repeated same-format events.
            if counts[event] > counts[other]:
                totals.append(float(match.group("ms")))
    return {"n": len(totals), "p95_ms": _p95(totals)}


def _judge(name: str, metrics: dict[str, Any], soft: float, hard: float, min_events: int, reasons: list[str]) -> str:
    p95 = metrics["p95_ms"]
    if metrics["n"] < min_events or p95 is None:
        return ALLOW
    if p95 >= hard:
        reasons.append(f"{name}_hard:p95={p95}ms>={hard:g}ms")
        return SKIP
    if p95 >= soft:
        reasons.append(f"{name}_soft:p95={p95}ms>={soft:g}ms")
        return CAPTURE_ONLY
    return ALLOW


def _append(path: Path, event: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > GUARD_LOG_MAX_BYTES:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass  # observation only: a full disk must not stop the beat's decision


def evaluate(
    now: datetime | None = None,
    *,
    dense_log: Path | None = None,
    hook_log: Path | None = None,
    guard_log: Path | None = None,
    record: bool = True,
) -> GuardDecision:
    """Return the beat's permission for this tick and record it in the guard log."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    thresholds: dict[str, Any] = {}
    reasons: list[str] = []
    metrics: dict[str, Any] = {"dense": {"n": 0, "p95_ms": None}, "hook": {"n": 0, "p95_ms": None}}
    try:
        thresholds = {
            "dense": [_float_env(DENSE_SOFT_ENV, DEFAULT_DENSE_SOFT_MS), _float_env(DENSE_HARD_ENV, DEFAULT_DENSE_HARD_MS)],
            "hook": [_float_env(HOOK_SOFT_ENV, DEFAULT_HOOK_SOFT_MS), _float_env(HOOK_HARD_ENV, DEFAULT_HOOK_HARD_MS)],
            "min_events": int(_float_env(MIN_EVENTS_ENV, DEFAULT_MIN_EVENTS)),
            "window_minutes": int(_float_env(WINDOW_ENV, DEFAULT_WINDOW_MINUTES)),
        }
        from memorymaster.core import hook_log as hook_log_module
        from memorymaster.recall import dense_recall

        since = current - timedelta(minutes=thresholds["window_minutes"])
        metrics["dense"] = _dense_metrics(dense_log if dense_log is not None else dense_recall.log_path(), since)
        metrics["hook"] = _hook_metrics(hook_log if hook_log is not None else hook_log_module._LOG_FILE, since)
        verdicts = [
            _judge("dense", metrics["dense"], *thresholds["dense"], thresholds["min_events"], reasons),
            _judge("hook", metrics["hook"], *thresholds["hook"], thresholds["min_events"], reasons),
        ]
        action = SKIP if SKIP in verdicts else CAPTURE_ONLY if CAPTURE_ONLY in verdicts else ALLOW
        if not reasons:
            reasons.append("within_bounds" if any(m["n"] >= thresholds["min_events"] for m in metrics.values())
                           else "insufficient_data")
    except Exception as exc:  # noqa: BLE001 - the guard must fail open, never block the beat on its own bug
        action, reasons = ALLOW, [f"guard_error:{type(exc).__name__}"]
    if record:
        _append(
            guard_log if guard_log is not None else guard_log_path(),
            {"ts": current.isoformat(timespec="seconds"), "action": action, "reasons": reasons,
             "dense": metrics["dense"], "hook": metrics["hook"], "thresholds": thresholds},
        )
    return GuardDecision(action, reasons, metrics)
