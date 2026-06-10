"""Observability metrics + dashboard panels (P1 WAL-discipline spec §2.10).

WHY: the §5 flip criteria (0 quick_check failures, WAL ≤ 64 MB, drain lag
≤ 6 h, busy errors ≤ baseline) and the §7 escalation tripwire ("WAL repeatedly
> 256 MB", "busy errors trending up") are only checkable if every steward
cycle PERSISTS one metrics snapshot and the dashboard SURFACES it. Without
these tests the metrics emit could silently stop and the 7-day dogfood
verdict — flip default ON vs escalate to the daemon design — would be made
on anecdote instead of data.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from memorymaster._storage_shared import busy_error_count, open_conn
from memorymaster.dashboard import create_dashboard_server
from memorymaster.dashboard_integrity import build_integrity_panel
from memorymaster.jobs import integrity
from memorymaster.service import MemoryService
from memorymaster.storage import SQLiteStore

METRIC_KEYS = {
    "wal_bytes",
    "checkpoint_busy",
    "checkpointed_frames",
    "quick_check_ok",
    "fk_orphans",
    "qdrant_drift",
    "spool_depth_files",
    "spool_depth_lines",
    "spool_drained",
    "spool_quarantined",
    "spool_lag_seconds",
    "busy_errors",
    "promotions_frozen",
}


def _metrics_events(store: Any) -> list[dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM events"
            " WHERE event_type = 'system' AND details = ?"
            " ORDER BY id",
            (integrity.MARKER_METRICS,),
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_run_cycle_persists_one_metrics_event_with_all_panel_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every run_cycle must record exactly ONE `integrity_metrics` system
    event carrying ALL §2.10 fields (WAL, checkpoint, quick_check, fk,
    qdrant drift, spool depth/drain/lag, busy errors).

    Intent: the flip criteria are evaluated as a per-cycle time series over
    7 dogfood days. A cycle that stops emitting — or drops a field — makes
    the corresponding tripwire (e.g. busy-error trend, WAL regrowth)
    unobservable, which is exactly the blindness §2.10 exists to remove.
    """
    monkeypatch.setenv("MEMORYMASTER_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    svc = MemoryService(tmp_path / "metrics.db")
    svc.init_db()

    result = svc.run_cycle()

    metrics = result["integrity_metrics"]
    assert METRIC_KEYS <= set(metrics), f"missing fields: {METRIC_KEYS - set(metrics)}"
    # The checkpoint truncated mid-cycle; later phase markers legitimately
    # regrow the WAL, so the emit reports the honest end-of-cycle size.
    assert result["integrity"]["checkpoint"]["wal_bytes"] == 0
    assert metrics["checkpoint_busy"] == 0
    assert isinstance(metrics["wal_bytes"], int) and metrics["wal_bytes"] >= 0
    assert metrics["quick_check_ok"] is True
    assert metrics["fk_orphans"] == 0
    assert metrics["qdrant_drift"] is None, "no Qdrant configured -> drift must be None, not fabricated"
    assert metrics["spool_depth_files"] == 0
    assert metrics["spool_depth_lines"] == 0
    assert isinstance(metrics["busy_errors"], int)
    assert metrics["promotions_frozen"] is False

    persisted = _metrics_events(svc.store)
    assert len(persisted) == 1, "exactly one metrics event per cycle"
    assert persisted[0] == metrics, "persisted payload must match the cycle report"

    svc.run_cycle()
    assert len(_metrics_events(svc.store)) == 2, "the series must grow once per cycle"


def test_metrics_emit_respects_integrity_disable_lever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEMORYMASTER_INTEGRITY_DISABLE=1 must also silence the metrics emit.

    Intent: spec §5 — the disable env is the no-deploy kill switch for the
    whole integrity machinery. If the metrics emit kept writing events while
    the operator believed integrity was off, the lever would be a lie (and
    the event write itself touches a DB the operator may be quiescing).
    """
    monkeypatch.setenv(integrity.ENV_DISABLE, "1")
    store = SQLiteStore(tmp_path / "disabled.db")
    store.init_db()
    res = integrity.emit_metrics(store, {})
    assert res == {"skipped": "disabled"}
    assert _metrics_events(store) == []


def test_busy_error_counter_counts_real_lock_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real "database is locked" failure inside open_conn must increment
    the process-local busy-error counter (and still raise).

    Intent: §7(d) — "busy-error counter trends up despite uniform 15 s
    timeouts" falsifies the WAL-discipline hypothesis. The counter must
    count REAL lock contention at the single choke point every writer now
    goes through, not rely on log greps. Counted via a genuine EXCLUSIVE
    lock held by a second connection (no monkeypatched sqlite3), with
    retries off so exactly one attempt = exactly one count.
    """
    monkeypatch.setenv("MEMORYMASTER_DB_RETRIES", "0")
    db = tmp_path / "contended.db"
    # Rollback-journal DB: open_conn's `PRAGMA journal_mode = WAL` conversion
    # needs an exclusive lock, so a held EXCLUSIVE transaction blocks it.
    setup = sqlite3.connect(str(db))
    setup.execute("CREATE TABLE t (x)")
    setup.commit()
    setup.close()
    holder = sqlite3.connect(str(db))
    holder.execute("BEGIN EXCLUSIVE")
    try:
        before = busy_error_count()
        with pytest.raises(sqlite3.OperationalError):
            open_conn(db)
        assert busy_error_count() == before + 1
    finally:
        holder.rollback()
        holder.close()


def test_busy_counter_ignores_non_busy_failures(tmp_path: Path) -> None:
    """Non-contention failures (e.g. unreadable path) must NOT increment.

    Intent: the tripwire compares busy counts flag-ON vs flag-OFF; polluting
    the counter with unrelated I/O errors would fake a contention trend and
    could trigger a false escalation to the daemon design.
    """
    before = busy_error_count()
    with pytest.raises(sqlite3.OperationalError):
        open_conn(tmp_path / "no-such-dir" / "x.db")
    assert busy_error_count() == before


@contextmanager
def _running_server(service: Any, operator_log: Path) -> Iterator[str]:
    server = create_dashboard_server(
        service=service,
        db_target="integrity-metrics-test.db",
        host="127.0.0.1",
        port=0,
        operator_log_jsonl=operator_log,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_api_integrity_serves_wal_spool_drift_busy_panels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/integrity must return the §2.10 panel JSON: live WAL bytes,
    spool depth, freeze state, busy errors, AND the last persisted cycle
    metrics (quick_check, fk orphans, drain lag, qdrant drift).

    Intent: the dashboard is where the operator reads the day-7 flip
    criteria — the spec names "WAL/spool/drift/busy panels" explicitly. A
    panel that renders but omits a field hides exactly one tripwire.
    """
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    db = tmp_path / "panel.db"
    svc = MemoryService(db)
    svc.init_db()
    # Seed one persisted cycle snapshot the way run_cycle does, with phase
    # results present so last_cycle carries real numbers.
    integrity.emit_metrics(
        svc.store,
        {
            "integrity": {
                "checkpoint": {"busy": 0, "log_frames": 0, "checkpointed_frames": 0, "wal_bytes": 0},
                "quick_check": {"ok": True, "rows": ["ok"]},
                "fk_check": {"orphans": 0, "by_table": {}},
            },
            "qdrant_reconcile": {"drift": 7},
            "spool_drain": {"drained": 3, "quarantined": 0, "lag_seconds": 1.5},
        },
    )

    operator_log = tmp_path / "operator.jsonl"
    operator_log.write_text("", encoding="utf-8")
    with _running_server(svc, operator_log) as base_url:
        with urllib.request.urlopen(f"{base_url}/api/integrity", timeout=3) as response:
            assert response.status == 200
            payload = json.loads(response.read().decode("utf-8"))

    assert payload["ok"] is True
    panel = payload["integrity"]
    assert panel["available"] is True
    # The requirement is "the panel serves the LIVE WAL size", not "the WAL
    # is zero": on Windows, truncate-on-last-close is silently skipped when
    # any outside process (AV / indexer) holds a handle on the file, so ==0
    # is an environment accident that flakes under full-suite load. Assert
    # parity with the real file instead — no writes happen between the
    # handler's measurement and ours.
    wal_file = Path(f"{db}-wal")
    actual_wal = wal_file.stat().st_size if wal_file.exists() else 0
    assert panel["wal_bytes"] == actual_wal
    assert panel["promotions_frozen"] is False
    assert isinstance(panel["busy_errors"], int)
    assert panel["spool"]["depth_files"] == 0
    assert panel["spool"]["depth_lines"] == 0
    last = panel["last_cycle"]
    assert last is not None, "persisted cycle metrics must surface"
    assert last["quick_check_ok"] is True
    assert last["fk_orphans"] == 0
    assert last["qdrant_drift"] == 7
    assert last["spool_lag_seconds"] == 1.5
    assert "at" in last


def test_panel_degrades_cleanly_without_sqlite_db() -> None:
    """A service without a SQLite db_path must yield available=False, not 500.

    Intent: the dashboard also fronts Postgres-backed and broken stores; the
    reliability panel is SQLite-specific (WAL files, spool dirs) and must say
    so instead of taking the whole dashboard down with an exception.
    """

    class NoDbService:
        store = object()

    panel = build_integrity_panel(NoDbService())
    assert panel == {"available": False, "reason": "no_sqlite_db"}


def test_setup_hooks_mirrors_wal_discipline_flag_into_hook_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install_hooks must mirror the operator's CURRENT machine-level
    MEMORYMASTER_WAL_DISCIPLINE into settings.json env — and write nothing
    when the operator never set it.

    Intent: spec §5 dogfood/rollback — hooks are fresh processes that read
    the flag from the Claude settings env. If setup pinned a hardcoded "0",
    a later `setx MEMORYMASTER_WAL_DISCIPLINE 1` would flip every writer
    EXCEPT the hooks, splitting the fleet across regimes (half spooling,
    half direct-writing) — the exact mixed state the umbrella flag forbids.
    """
    from memorymaster import setup_hooks

    llm_config = {"provider": "google", "api_key": "", "model": ""}

    monkeypatch.setattr(setup_hooks, "CLAUDE_DIR", tmp_path / "with-flag" / ".claude")
    monkeypatch.setenv("MEMORYMASTER_WAL_DISCIPLINE", "1")
    setup_hooks.install_hooks(llm_config)
    settings = json.loads(
        (tmp_path / "with-flag" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["env"]["MEMORYMASTER_WAL_DISCIPLINE"] == "1"
    hooks_dir = tmp_path / "with-flag" / ".claude" / "hooks"
    assert (hooks_dir / "memorymaster-recall.py").exists(), "hook templates must regenerate"
    assert (hooks_dir / "memorymaster-auto-ingest.py").exists()

    monkeypatch.setattr(setup_hooks, "CLAUDE_DIR", tmp_path / "no-flag" / ".claude")
    monkeypatch.delenv("MEMORYMASTER_WAL_DISCIPLINE", raising=False)
    setup_hooks.install_hooks(llm_config)
    settings = json.loads(
        (tmp_path / "no-flag" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert "MEMORYMASTER_WAL_DISCIPLINE" not in settings["env"], (
        "unset flag must not be pinned — the in-code default (OFF) governs"
    )
