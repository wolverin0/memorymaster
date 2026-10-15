"""Which entry of an ``active_tasks.md`` may drive the look-ahead briefing (T-1433).

The look-ahead hook briefs the model on "the" active task. Picking the first
``in_progress`` (else first ``pending``) entry regardless of age or owner briefed
a lane on a months-old task that was never closed, and on another lane's task.

A task is excluded when:

* it is older than the freshness bound (default :data:`DEFAULT_MAX_AGE_HOURS`);
* its lease (``lease_until`` / ``lease_expires_at``) has expired;
* it names a ``cwd``/``workspace`` that does not contain, or sit under, the caller's cwd;
* it names an ``owner`` other than the caller's lane.

Age is measured from the newest of ``updated_at`` / ``started_at`` / ``claimed_at`` /
``dispatched_at`` in the task's yaml block (``dispatched_at`` is what the dispatcher
writes today); a task with none of them falls back to the file's mtime. Standard
library only; unparsable values degrade to "unknown age", never to an exception.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

MAX_AGE_ENV = "MEMORYMASTER_ACTIVE_TASK_MAX_AGE_HOURS"
# 48h: a task a lane is really working on is dispatched, run and closed inside a working
# day or two, so a two-day bound still covers a task carried over a night or a weekend
# edit of the file, while months-old "in_progress" leftovers are far beyond it.
DEFAULT_MAX_AGE_HOURS = 48.0
_FRESH_KEYS = ("updated_at", "started_at", "claimed_at", "dispatched_at")
_LEASE_KEYS = ("lease_until", "lease_expires_at")
_CWD_KEYS = ("cwd", "workspace")


def max_age_hours() -> float:
    """Freshness bound in hours from the environment; 0 or negative disables the age check."""
    try:
        return float(os.environ.get(MAX_AGE_ENV, DEFAULT_MAX_AGE_HOURS))
    except ValueError:
        return DEFAULT_MAX_AGE_HOURS


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parts(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").lower().split("/") if p]


def _related(task_cwd: str, cwd: str) -> bool:
    a, b = _parts(task_cwd), _parts(cwd)
    n = min(len(a), len(b))
    return bool(a) and bool(b) and a[:n] == b[:n]  # one is the other or an ancestor of it


def exclusion_reason(
    task: dict,
    *,
    now: datetime,
    file_mtime: datetime | None,
    max_age: float,
    cwd: str = "",
    lane: str = "",
) -> str | None:
    """Why ``task`` must not drive a briefing, or None when it may."""
    fields = task.get("yaml") or {}
    for key in _LEASE_KEYS:
        lease = _parse(fields.get(key))
        if lease is not None and lease < now:
            return f"lease expired ({key})"
    if max_age > 0:
        stamps = [t for t in (_parse(fields.get(k)) for k in _FRESH_KEYS) if t is not None]
        newest = max(stamps) if stamps else file_mtime
        if newest is not None and (now - newest).total_seconds() > max_age * 3600:
            return f"older than {max_age:g}h"
    task_cwd = next((str(fields[k]) for k in _CWD_KEYS if fields.get(k)), "")
    if cwd and task_cwd and not _related(task_cwd, cwd):
        return "other cwd"
    owner = str(fields.get("owner") or "")
    if lane and owner and owner.lower() != lane.lower():
        return "other lane"
    return None


def select_active(
    tasks: list[dict],
    *,
    file_mtime: float | None = None,
    now: datetime | None = None,
    max_age: float | None = None,
    cwd: str = "",
    lane: str = "",
) -> tuple[dict | None, list[dict]]:
    """The active task (first fresh ``in_progress``, else first fresh ``pending``) and the skipped ones.

    ``skipped`` lists ``{"id", "reason"}`` for every in_progress/pending task that was excluded.
    """
    now = now or datetime.now(timezone.utc)
    bound = max_age_hours() if max_age is None else max_age
    mtime = datetime.fromtimestamp(file_mtime, timezone.utc) if file_mtime is not None else None
    skipped: list[dict] = []
    eligible: list[dict] = []
    for task in tasks:
        if task.get("status") not in ("in_progress", "pending"):
            continue
        reason = exclusion_reason(task, now=now, file_mtime=mtime, max_age=bound, cwd=cwd, lane=lane)
        if reason:
            skipped.append({"id": task.get("id"), "reason": reason})
        else:
            eligible.append(task)
    active = next((t for t in eligible if t["status"] == "in_progress"), None)
    active = active or next((t for t in eligible if t["status"] == "pending"), None)
    return active, skipped


_HEADER = re.compile(r"^##\s+(?:Task:\s+)?(.+?)\s*$", re.MULTILINE)
_YAML = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


def parse_tasks(text: str) -> list[dict]:
    """Split an ``active_tasks.md`` into ``{id, title, status, narrative, yaml}`` entries."""
    headers = [(m.start(), m.end(), m.group(1).strip()) for m in _HEADER.finditer(text)]
    tasks: list[dict] = []
    for index, (_start, end, title) in enumerate(headers):
        body = text[end : headers[index + 1][0] if index + 1 < len(headers) else len(text)]
        fields: dict[str, str] = {}
        block = _YAML.search(body)
        if block:
            for line in block.group(1).splitlines():
                pair = re.match(r"^([A-Za-z0-9_]+)\s*:\s*(.*)$", line.strip())
                if pair:
                    fields[pair.group(1)] = pair.group(2).strip().strip("\"'")
        id_match = re.search(r"\b(T-?\d+)\b", title)  # "T-001 · Title" or "Task: foo"
        tasks.append({
            "id": id_match.group(1) if id_match else f"task-{index + 1}",
            "title": title,
            "status": fields.get("status", "unknown"),
            "narrative": _YAML.sub("", body).strip(),
            "yaml": fields,
        })
    return tasks


def fresh_active_task(path: str | os.PathLike, *, cwd: str = "", lane: str = "") -> tuple[str | None, str | None]:
    """``(title, narrative)`` of the fresh active task in ``path``, or ``(None, None)``.

    Drop-in for a hook's own "first in_progress, else first pending" parser: same pair,
    but stale, expired-lease and foreign-cwd/lane tasks are skipped. Never raises.
    """
    try:
        source = Path(path)
        tasks = parse_tasks(source.read_text(encoding="utf-8"))
        active, _skipped = select_active(tasks, file_mtime=source.stat().st_mtime, cwd=cwd, lane=lane)
    except (OSError, ValueError):
        return None, None
    return (active["title"], active["narrative"]) if active else (None, None)
