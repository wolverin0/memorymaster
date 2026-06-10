"""Scheduled integrity steward phase (P1 WAL-discipline spec §2.5).

Four operations, wired at the end of ``service.run_cycle`` and exposed via
the ``integrity`` CLI subcommand:

1. **Checkpoint** (every cycle): ``PRAGMA wal_checkpoint(TRUNCATE)`` on a
   dedicated 30 s busy-timeout connection. The live DB accumulated a 1.44 GB
   WAL because passive auto-checkpoint is permanently starved by ~12
   reader/writer processes (spec F4) — an explicit TRUNCATE per cycle is the
   only thing that actually retires frames. WAL still > 256 MB afterwards
   emits an ``integrity_wal_oversize`` event (escalation tripwire, spec §7).
2. **quick_check** (1/day): on a read-only connection. Any non-``ok`` row
   writes the ``<db>.integrity-failed`` sentinel, emits
   ``integrity_check_failed``, and **freezes steward promotions** — the
   validator/deterministic phases check the sentinel and no-op so a broken
   btree is never written through. Never auto-destructive: the sentinel is
   removed by the operator, not by a later passing check.
3. **foreign_key_check** (1/day, read-only): orphan count as a metric.
   Non-zero after the step-5 repair is a regression alert (spec F10).
4. **VACUUM INTO snapshot** (1/week): rotated, keep 3, dir configurable via
   ``MEMORYMASTER_SNAPSHOT_DIR`` (default ``~/.memorymaster/snapshots/`` —
   outside the OneDrive-synced tree). Replaces ad-hoc 3.6 GB ``.bak`` copies.

Throttle stamps live in the append-only ``events`` table (event_type
``system`` + a details marker) — no new table, no schema change, parity-safe.
Everything here is additive and default-on; ``MEMORYMASTER_INTEGRITY_DISABLE=1``
is the rollback lever (spec §5).
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster._storage_shared import busy_error_count, connect_ro, open_conn, utc_now

logger = logging.getLogger(__name__)

ENV_DISABLE = "MEMORYMASTER_INTEGRITY_DISABLE"

CHECKPOINT_BUSY_TIMEOUT_MS = 30000
WAL_OVERSIZE_BYTES = 256 * 1024 * 1024

SENTINEL_SUFFIX = ".integrity-failed"

# Throttle/metric markers recorded as `system` events (events is append-only;
# MAX(created_at) per marker is the stamp).
MARKER_QUICK_CHECK = "integrity_quick_check"
MARKER_FK_CHECK = "integrity_fk_check"
MARKER_VACUUM = "integrity_vacuum_snapshot"
MARKER_CHECK_FAILED = "integrity_check_failed"
MARKER_WAL_OVERSIZE = "integrity_wal_oversize"
MARKER_METRICS = "integrity_metrics"

QUICK_CHECK_INTERVAL_HOURS = 24
FK_CHECK_INTERVAL_HOURS = 24
VACUUM_INTERVAL_HOURS = 24 * 7


def sentinel_path(db_path: str | Path) -> Path:
    """Path of the promotion-freeze sentinel for a DB file."""
    return Path(f"{db_path}{SENTINEL_SUFFIX}")


def promotions_frozen(db_path: str | Path) -> bool:
    """True when a failed quick_check has frozen steward promotions."""
    return sentinel_path(db_path).exists()


def promotions_frozen_for(store) -> bool:
    """Store-level freeze probe — False for non-SQLite stores (no db_path)."""
    db_path = getattr(store, "db_path", None)
    return bool(db_path) and promotions_frozen(db_path)


def _wal_bytes(db_path: str | Path) -> int:
    wal = Path(f"{db_path}-wal")
    try:
        return wal.stat().st_size
    except OSError:
        return 0


def _record(store, details: str, payload: dict[str, object]) -> None:
    """Record a `system` event marker; never raises (a corrupt DB may refuse
    the write — the sentinel/file-level signal must still land)."""
    with contextlib.suppress(Exception):
        store.record_event(
            claim_id=None,
            event_type="system",
            details=details,
            payload=payload,
        )


def _last_marker_at(db_path: str | Path, details: str) -> datetime | None:
    """Most recent marker timestamp, or None if never recorded/unreadable."""
    try:
        with closing(connect_ro(db_path)) as conn:
            row = conn.execute(
                "SELECT MAX(created_at) FROM events WHERE event_type = 'system' AND details = ?",
                (details,),
            ).fetchone()
    except sqlite3.Error:
        return None
    raw = row[0] if row else None
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _due(db_path: str | Path, marker: str, *, hours: int, now: datetime | None) -> bool:
    last = _last_marker_at(db_path, marker)
    if last is None:
        return True
    ref = now or datetime.now(timezone.utc)
    return (ref - last) >= timedelta(hours=hours)


def checkpoint(store, db_path: str | Path) -> dict[str, object]:
    """``PRAGMA wal_checkpoint(TRUNCATE)`` on a dedicated 30 s-timeout conn."""
    conn = open_conn(db_path, busy_ms=CHECKPOINT_BUSY_TIMEOUT_MS)
    try:
        busy, log_frames, checkpointed = conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
    finally:
        conn.close()
    wal_bytes = _wal_bytes(db_path)
    result: dict[str, object] = {
        "busy": busy,
        "log_frames": log_frames,
        "checkpointed_frames": checkpointed,
        "wal_bytes": wal_bytes,
    }
    logger.info(
        "integrity checkpoint: busy=%s log=%s checkpointed=%s wal_bytes=%s",
        busy, log_frames, checkpointed, wal_bytes,
    )
    if wal_bytes > WAL_OVERSIZE_BYTES:
        result["oversize"] = True
        _record(store, MARKER_WAL_OVERSIZE, dict(result))
    return result


def quick_check(
    store,
    db_path: str | Path,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, object]:
    """Daily ``PRAGMA quick_check``; non-ok freezes promotions via sentinel."""
    if not force and not _due(db_path, MARKER_QUICK_CHECK, hours=QUICK_CHECK_INTERVAL_HOURS, now=now):
        return {"skipped": "throttled"}
    try:
        with closing(connect_ro(db_path)) as conn:
            rows = [str(r[0]) for r in conn.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.Error as exc:
        # A DB too broken to even run the pragma IS a failed check.
        rows = [f"quick_check_error: {exc}"]
    ok = rows == ["ok"]
    if not ok:
        sentinel = sentinel_path(db_path)
        with contextlib.suppress(OSError):
            sentinel.write_text(
                f"quick_check failed at {utc_now()}\n" + "\n".join(rows[:20]) + "\n",
                encoding="utf-8",
            )
        _record(store, MARKER_CHECK_FAILED, {"rows": rows[:10]})
        with contextlib.suppress(Exception):
            from memorymaster.webhook import fire_webhook

            fire_webhook("integrity_check_failed", {"db": str(db_path), "rows": rows[:10]})
        logger.error("integrity quick_check FAILED for %s: %s", db_path, rows[:5])
    _record(store, MARKER_QUICK_CHECK, {"ok": ok})
    return {"ok": ok, "rows": rows[:10]}


def fk_check(
    store,
    db_path: str | Path,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, object]:
    """Daily read-only ``PRAGMA foreign_key_check`` — orphan count metric."""
    if not force and not _due(db_path, MARKER_FK_CHECK, hours=FK_CHECK_INTERVAL_HOURS, now=now):
        return {"skipped": "throttled"}
    try:
        with closing(connect_ro(db_path)) as conn:
            rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        return {"error": str(exc)}
    by_table = Counter(str(r[0]) for r in rows)
    result: dict[str, object] = {"orphans": len(rows), "by_table": dict(by_table)}
    if rows:
        logger.warning("integrity fk_check: %s orphan rows (%s)", len(rows), dict(by_table))
    _record(store, MARKER_FK_CHECK, dict(result))
    return result


def vacuum_snapshot(
    store,
    db_path: str | Path,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, object]:
    """Weekly ``VACUUM INTO`` snapshot, rotated keep-3 (snapshot.vacuum_into)."""
    if not force and not _due(db_path, MARKER_VACUUM, hours=VACUUM_INTERVAL_HOURS, now=now):
        return {"skipped": "throttled"}
    from memorymaster import snapshot

    info = snapshot.vacuum_into(db_path)
    _record(store, MARKER_VACUUM, dict(info))
    return info


def status(store, db_path: str | Path) -> dict[str, object]:
    """Operator-facing snapshot: WAL size, sentinel, last phase runs."""
    from memorymaster import snapshot

    snap_dir = snapshot.vacuum_dir_for(db_path)
    snaps = sorted(p.name for p in snap_dir.glob("mm-*.db")) if snap_dir.exists() else []
    last = {
        marker: (stamp.isoformat() if stamp else None)
        for marker, stamp in (
            (MARKER_QUICK_CHECK, _last_marker_at(db_path, MARKER_QUICK_CHECK)),
            (MARKER_FK_CHECK, _last_marker_at(db_path, MARKER_FK_CHECK)),
            (MARKER_VACUUM, _last_marker_at(db_path, MARKER_VACUUM)),
            (MARKER_CHECK_FAILED, _last_marker_at(db_path, MARKER_CHECK_FAILED)),
        )
    }
    return {
        "db": str(db_path),
        "wal_bytes": _wal_bytes(db_path),
        "promotions_frozen": promotions_frozen(db_path),
        "sentinel": str(sentinel_path(db_path)),
        "last_runs": last,
        "snapshot_dir": str(snap_dir),
        "snapshots": snaps,
    }


def run(
    store,
    *,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Full steward phase: checkpoint every cycle + throttled daily/weekly ops.

    Each sub-phase is isolated — one failing must not stop the others, and
    nothing here may ever break the surrounding cycle.
    """
    if os.environ.get(ENV_DISABLE, "").strip() == "1":
        return {"skipped": "disabled"}
    db_path = db_path or getattr(store, "db_path", None)
    if not db_path or not Path(db_path).exists():
        return {"skipped": "no_sqlite_db"}

    result: dict[str, object] = {}
    phases = (
        ("checkpoint", lambda: checkpoint(store, db_path)),
        ("quick_check", lambda: quick_check(store, db_path, now=now)),
        ("fk_check", lambda: fk_check(store, db_path, now=now)),
        ("vacuum_snapshot", lambda: vacuum_snapshot(store, db_path, now=now)),
    )
    for name, phase in phases:
        try:
            result[name] = phase()
        except Exception as exc:
            logger.warning("integrity phase %s failed: %s", name, exc)
            result[name] = {"error": str(exc)}
    result["promotions_frozen"] = promotions_frozen(db_path)
    return result


def _phase_dict(container: dict[str, object] | None, key: str) -> dict[str, object]:
    """Sub-result of a cycle phase, or {} when skipped/throttled/errored."""
    value = (container or {}).get(key)
    return value if isinstance(value, dict) else {}


def emit_metrics(
    store,
    cycle_result: dict[str, object],
    *,
    db_path: str | Path | None = None,
) -> dict[str, object]:
    """Persist the §2.10 per-cycle observability snapshot as ONE system event.

    One ``integrity_metrics`` event per steward cycle carrying: WAL bytes,
    checkpoint result, quick_check status, fk orphan count, qdrant drift,
    spool depth + drain lag, busy-error count. These are the §5 flip criteria
    AND the §7 escalation tripwire inputs — without a persisted series, "WAL
    repeatedly > 256 MB" or "busy errors trending up" stay anecdotal.
    Throttled sub-phases report ``None`` for this cycle (their own daily
    markers carry the last real value).
    """
    if os.environ.get(ENV_DISABLE, "").strip() == "1":
        return {"skipped": "disabled"}
    db_path = db_path or getattr(store, "db_path", None)
    if not db_path or not Path(db_path).exists():
        return {"skipped": "no_sqlite_db"}
    from memorymaster import spool

    integ = cycle_result.get("integrity")
    integ = integ if isinstance(integ, dict) else {}
    ck = _phase_dict(integ, "checkpoint")
    qc = _phase_dict(integ, "quick_check")
    fk = _phase_dict(integ, "fk_check")
    qd = cycle_result.get("qdrant_reconcile")
    qd = qd if isinstance(qd, dict) else {}
    sd = cycle_result.get("spool_drain")
    sd = sd if isinstance(sd, dict) else {}
    depth = spool.pending_depth(db_path)
    metrics: dict[str, object] = {
        "wal_bytes": _wal_bytes(db_path),
        "checkpoint_busy": ck.get("busy"),
        "checkpointed_frames": ck.get("checkpointed_frames"),
        "quick_check_ok": qc.get("ok"),
        "fk_orphans": fk.get("orphans"),
        "qdrant_drift": qd.get("drift"),
        "spool_depth_files": depth["files"],
        "spool_depth_lines": depth["lines"],
        "spool_drained": sd.get("drained"),
        "spool_quarantined": sd.get("quarantined"),
        "spool_lag_seconds": sd.get("lag_seconds"),
        "busy_errors": busy_error_count(),
        "promotions_frozen": promotions_frozen(db_path),
    }
    _record(store, MARKER_METRICS, metrics)
    return metrics
