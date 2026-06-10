"""Integrity steward phase (P1 WAL-discipline spec §2.5).

WHY: the package had ZERO checkpoint/integrity discipline (spec F3) — a
1.44 GB WAL accumulated against the 3.47 GB live DB because passive
auto-checkpoint is starved by ~12 concurrent processes (F4), and the
2026-06-05 index corruption went undetected until recall crashed on it.
These tests pin the new scheduled discipline: every cycle truncates the WAL,
corruption is detected within a day and FREEZES steward promotions (writing
through a broken btree compounds damage), a weekly VACUUM INTO snapshot
bounds worst-case recovery, and the daily/weekly throttles keep the 3.47 GB
production scans from running on every 6 h cycle.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster import snapshot
from memorymaster._storage_shared import open_conn
from memorymaster.jobs import deterministic, integrity, validator
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService
from memorymaster.storage import SQLiteStore


@pytest.fixture()
def store(tmp_path: Path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "integrity.db")
    s.init_db()
    return s


def _grow_wal(db_path: str) -> sqlite3.Connection:
    """Write enough to leave frames in the -wal; keep the conn open so
    close-time auto-checkpoint cannot retire them before the test runs."""
    conn = open_conn(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS bulk (x TEXT)")
    conn.executemany("INSERT INTO bulk (x) VALUES (?)", [("y" * 100,)] * 500)
    conn.commit()
    return conn


def test_checkpoint_truncates_wal(store: SQLiteStore) -> None:
    """wal_checkpoint(TRUNCATE) must retire ALL frames — wal_bytes ends at 0.

    Intent: spec F4 — passive auto-checkpoint never wins on the live DB, so
    the steward's explicit TRUNCATE is the only mechanism that keeps the WAL
    bounded. PASSIVE/FULL would merely copy frames without truncating the
    file; only a 0-byte result proves TRUNCATE semantics survived.
    """
    writer = _grow_wal(store.db_path)
    try:
        assert integrity._wal_bytes(store.db_path) > 0, "test setup must leave WAL frames"
        res = integrity.checkpoint(store, store.db_path)
        assert res["busy"] == 0
        assert res["wal_bytes"] == 0, f"WAL must be truncated to 0 bytes, got {res['wal_bytes']}"
        assert res["checkpointed_frames"] >= 0
    finally:
        writer.close()


def test_checkpoint_emits_oversize_event(store: SQLiteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A WAL still above the threshold AFTER checkpointing must emit the
    `integrity_wal_oversize` event.

    Intent: this event is an input to the §7 escalation tripwire — 'WAL
    repeatedly > 256 MB because TRUNCATE never wins under pane churn'
    falsifies the WAL-discipline hypothesis and escalates to the daemon
    design. Without the event the failure mode is invisible. (Threshold is
    monkeypatched below the post-truncate size so the emit path runs without
    stalling a real 30 s busy checkpoint.)
    """
    monkeypatch.setattr(integrity, "WAL_OVERSIZE_BYTES", -1)
    res = integrity.checkpoint(store, store.db_path)
    assert res.get("oversize") is True
    with store.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'system' AND details = ?",
            (integrity.MARKER_WAL_OVERSIZE,),
        ).fetchone()[0]
    assert count == 1


def test_quick_check_ok_no_sentinel(store: SQLiteStore) -> None:
    """A healthy DB passes quick_check and must NOT create the freeze sentinel.

    Intent: the sentinel halts all steward promotions fleet-wide — a false
    positive on a healthy DB would silently stop memory governance, which is
    exactly the class of silent regression the spec's judges flagged.
    """
    res = integrity.quick_check(store, store.db_path, force=True)
    assert res["ok"] is True
    assert res["rows"] == ["ok"]
    assert not integrity.promotions_frozen(store.db_path)


def test_quick_check_corruption_writes_sentinel_and_freezes(tmp_path: Path) -> None:
    """An injected-corruption quick_check failure must write the sentinel and
    freeze promotions — and never auto-delete the corrupt file.

    Intent: spec §2.5.2 — detection must be loud (sentinel + freeze) but
    never destructive. The 2026-06-05 corruption was only found when recall
    crashed; this is the 'within a day, before the steward writes through a
    broken btree' guarantee.
    """
    healthy = SQLiteStore(tmp_path / "victim.db")
    healthy.init_db()
    writer = _grow_wal(healthy.db_path)
    # Locate the populated `bulk` table's root btree page so the corruption
    # lands on a page quick_check actually walks (random offsets can hit
    # free pages and pass).
    rootpage = writer.execute(
        "SELECT rootpage FROM sqlite_master WHERE name = 'bulk'"
    ).fetchone()[0]
    page_size = writer.execute("PRAGMA page_size").fetchone()[0]
    # Fold the WAL into the main file BEFORE corrupting it — otherwise
    # quick_check reads the healthy page images straight from the WAL and
    # the injected corruption is invisible.
    busy = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    writer.close()
    assert busy == 0, "test setup: checkpoint must fully fold the WAL"

    db_file = Path(healthy.db_path)
    blob = bytearray(db_file.read_bytes())
    off = (rootpage - 1) * page_size
    blob[off:off + page_size] = b"\xff" * page_size
    db_file.write_bytes(bytes(blob))

    corrupt_store = SQLiteStore(db_file)
    res = integrity.quick_check(corrupt_store, corrupt_store.db_path, force=True)
    assert res["ok"] is False
    assert res["rows"], "failure rows/diagnostics must be reported"
    assert integrity.promotions_frozen(corrupt_store.db_path)
    sentinel = integrity.sentinel_path(corrupt_store.db_path)
    assert sentinel.exists()
    assert "quick_check" in sentinel.read_text(encoding="utf-8")
    assert db_file.exists(), "integrity phase must never delete/modify the DB file"


def test_promotion_freeze_noops_validator_and_deterministic(tmp_path: Path) -> None:
    """With the sentinel present, validator and deterministic must no-op;
    removing it restores promotion — proving the freeze is the ONLY blocker.

    Intent: spec §2.5.2 — after a failed quick_check, every steward write
    against the suspect DB risks compounding btree damage. The freeze must
    actually stop status transitions, not just be reported.
    """
    svc = MemoryService(tmp_path / "freeze.db")
    svc.init_db()
    claim = svc.ingest(
        text="Server hostname is alpha.internal for the build farm",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="hostname")],
        subject="build-farm",
        predicate="hostname_note",
        object_value="alpha.internal",
    )
    assert claim.status == "candidate"

    sentinel = integrity.sentinel_path(svc.store.db_path)
    sentinel.write_text("quick_check failed (test)", encoding="utf-8")

    vres = validator.run(svc.store, min_citations=1, min_score=0.1)
    assert vres["frozen"] == 1
    assert vres["confirmed"] == 0
    dres = deterministic.run(svc.store, workspace_root=tmp_path)
    assert dres["frozen"] == 1
    assert dres["checked"] == 0
    assert svc.store.get_claim(claim.id).status == "candidate", "frozen validator must not transition claims"

    sentinel.unlink()
    vres2 = validator.run(svc.store, min_citations=1, min_score=0.1)
    assert "frozen" not in vres2
    assert svc.store.get_claim(claim.id).status == "confirmed", "unfreezing must restore normal promotion"


def test_vacuum_into_rotation_keeps_3(store: SQLiteStore, tmp_path: Path) -> None:
    """vacuum_into keeps exactly the 3 newest dated snapshots; same-day reruns reuse.

    Intent: spec §2.5.4 — snapshots replace the ad-hoc 3.6 GB .bak copies.
    Unbounded rotation would silently fill the disk the DB lives on; fewer
    than 3 shrinks the recovery window after an undetected-corruption week.
    """
    dest = tmp_path / "snaps"
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for days in range(4):
        info = snapshot.vacuum_into(store.db_path, dest, now=base + timedelta(days=days))
        assert info["created"] is True
    names = sorted(p.name for p in dest.glob("mm-*.db"))
    assert names == ["mm-20260602.db", "mm-20260603.db", "mm-20260604.db"]

    again = snapshot.vacuum_into(store.db_path, dest, now=base + timedelta(days=3))
    assert again["created"] is False, "same-day rerun must reuse, not rebuild"

    with sqlite3.connect(f"file:{dest / 'mm-20260604.db'}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_quick_check_daily_throttle(store: SQLiteStore) -> None:
    """quick_check runs at most once per 24 h unless forced.

    Intent: spec §2.5.2 throttle — a full quick_check scans every btree page
    of the 3.47 GB production DB; running it on every 6 h cycle would turn
    the integrity phase itself into the contention problem it exists to fix.
    """
    first = integrity.quick_check(store, store.db_path)
    assert first["ok"] is True
    second = integrity.quick_check(store, store.db_path)
    assert second == {"skipped": "throttled"}
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    third = integrity.quick_check(store, store.db_path, now=later)
    assert third["ok"] is True


def test_vacuum_weekly_throttle(store: SQLiteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """vacuum_snapshot runs at most once per 7 days unless forced.

    Intent: VACUUM INTO rewrites the entire DB (multi-GB I/O in production);
    the weekly cadence is the spec's cost/recovery balance. The phase wrapper
    owns the throttle so run_cycle can call it unconditionally every cycle.
    """
    monkeypatch.setenv("MEMORYMASTER_SNAPSHOT_DIR", str(tmp_path / "weekly"))
    first = integrity.vacuum_snapshot(store, store.db_path)
    assert first["created"] is True
    second = integrity.vacuum_snapshot(store, store.db_path)
    assert second == {"skipped": "throttled"}
    later = datetime.now(timezone.utc) + timedelta(days=8)
    third = integrity.vacuum_snapshot(store, store.db_path, now=later)
    assert "skipped" not in third


def test_fk_check_reports_orphans(store: SQLiteStore) -> None:
    """fk_check must surface orphan FK rows as a per-table metric.

    Intent: spec F10 — 401 orphan rows sat unnoticed on the live DB as
    recovery collateral. After the step-5 repair, any non-zero count is a
    regression alert; a fk_check that can't see seeded orphans alerts nobody.
    """
    clean = integrity.fk_check(store, store.db_path, force=True)
    assert clean["orphans"] == 0

    with store.connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO events (claim_id, event_type, details, created_at)"
            " VALUES (999999, 'system', 'orphan-seed', '2026-06-01T00:00:00+00:00')"
        )
        conn.commit()
    res = integrity.fk_check(store, store.db_path, force=True)
    assert res["orphans"] == 1
    assert res["by_table"] == {"events": 1}


def test_run_disabled_via_env(store: SQLiteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """MEMORYMASTER_INTEGRITY_DISABLE=1 must skip the whole phase.

    Intent: spec §5 rollback lever — if a checkpoint ever misbehaves in
    production the operator needs a kill switch that does not require a
    deploy. The phase must do NOTHING (no markers, no checkpoint) when set.
    """
    monkeypatch.setenv(integrity.ENV_DISABLE, "1")
    res = integrity.run(store)
    assert res == {"skipped": "disabled"}
    with store.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'system' AND details LIKE 'integrity%'"
        ).fetchone()[0]
    assert count == 0


def test_run_cycle_includes_integrity_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cycle must execute the integrity phase and report its results.

    Intent: spec §2.5 wires integrity as a steward phase precisely so it
    needs no new resident process or cron — if run_cycle stops reporting the
    'integrity' key, the discipline silently stopped running fleet-wide.
    """
    monkeypatch.setenv("MEMORYMASTER_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    svc = MemoryService(tmp_path / "cycle.db")
    svc.init_db()
    result = svc.run_cycle()
    assert "integrity" in result
    phase = result["integrity"]
    assert "checkpoint" in phase
    assert phase["checkpoint"]["wal_bytes"] == 0
    assert phase["promotions_frozen"] is False


def test_cli_integrity_checkpoint_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`memorymaster integrity --checkpoint --json` must run and report JSON.

    Intent: the hermes-sync piggyback (windows-hermes-sync.ps1 step 3) parses
    exactly this envelope (`data.checkpoint.wal_bytes` etc.) twice daily —
    a renamed key or missing subcommand breaks the scheduled checkpoint
    silently inside a try/catch that is deliberately non-fatal.
    """
    from memorymaster.surfaces.cli import main

    db = tmp_path / "cli.db"
    SQLiteStore(db).init_db()
    rc = main(["--db", str(db), "--json", "integrity", "--checkpoint"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    ck = out["data"]["checkpoint"]
    assert {"busy", "log_frames", "checkpointed_frames", "wal_bytes"} <= set(ck)
    assert ck["wal_bytes"] == 0


def test_cli_integrity_status(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """`integrity --status` must report WAL size, freeze state and last runs.

    Intent: §5 flip criteria are checked from these fields at day 7; the
    operator runbook reads this status before/after the supervised TRUNCATE.
    """
    from memorymaster.surfaces.cli import main

    monkeypatch.setenv("MEMORYMASTER_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    db = tmp_path / "cli-status.db"
    SQLiteStore(db).init_db()
    rc = main(["--db", str(db), "--json", "integrity", "--status"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    data = out["data"]
    assert data["promotions_frozen"] is False
    assert "wal_bytes" in data
    assert integrity.MARKER_QUICK_CHECK in data["last_runs"]


def test_snapshot_dir_default_outside_db_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default snapshot dir must be under the user home, not next to the DB.

    Intent: spec §2.5.4 — the live DB sits inside a OneDrive-synced tree
    (F16); parking multi-GB weekly snapshots beside it would triple the sync
    surface AND die with the same disk. Env override must win when set.
    """
    monkeypatch.delenv("MEMORYMASTER_SNAPSHOT_DIR", raising=False)
    default = snapshot.default_vacuum_dir()
    assert default == Path.home() / ".memorymaster" / "snapshots"
    monkeypatch.setenv("MEMORYMASTER_SNAPSHOT_DIR", str(Path("X:/custom/snaps")))
    assert snapshot.default_vacuum_dir() == Path("X:/custom/snaps")
    assert os.environ["MEMORYMASTER_SNAPSHOT_DIR"]  # guard against typo'd env name
