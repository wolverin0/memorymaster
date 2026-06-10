"""Qdrant reconciliation steward phase (P1 WAL-discipline spec §2.7).

WHY: Qdrant sync is fire-and-forget (spec F11) — `_qdrant_sync` swallows
exceptions and `_qdrant_post_cycle_sync` only re-upserts recently-changed
claims, so the vector index silently drifts from SQLite truth: missed
upserts accumulate and points for archived/deleted claims linger forever,
degrading recall quality with zero signal. These tests pin the
reconciliation discipline: an observable daily drift metric, a threshold
that keeps a full multi-thousand-claim re-embed from running on every 6 h
cycle, convergence (sync_all + orphan-point delete) on breach, and a clean
skip on machines without Qdrant — which is most of them.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.govern.jobs import qdrant_reconcile
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService
from memorymaster.stores.storage import SQLiteStore


class FakeQdrant:
    """In-memory stand-in for QdrantBackend — records what reconcile asks of it."""

    def __init__(self, count: int = 0, point_claim_ids: list[int] | None = None) -> None:
        self.count = count
        self.point_claim_ids = list(point_claim_ids or [])
        self.sync_all_calls = 0
        self.deleted: list[int] = []

    def count_points(self) -> int | None:
        return self.count

    def list_point_claim_ids(self, *, batch_size: int = 1000) -> list[int] | None:
        return list(self.point_claim_ids)

    def delete_claim(self, claim_id: int) -> bool:
        self.deleted.append(claim_id)
        return True

    def upsert_claim(self, claim, source: str = "memorymaster") -> bool:
        return True

    def sync_all(self, store, *, batch_size: int = 50) -> dict[str, int]:
        self.sync_all_calls += 1
        return {"total": 5, "synced": 5, "skipped": 0, "errors": 0}


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryService:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv(qdrant_reconcile.ENV_DRIFT_MAX, raising=False)
    # run_cycle's integrity phase vacuums a snapshot — keep it out of ~ during tests.
    monkeypatch.setenv("MEMORYMASTER_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    service = MemoryService(tmp_path / "reconcile.db")
    service.init_db()
    return service


def _ingest_claims(svc: MemoryService, n: int) -> list[int]:
    ids = []
    for i in range(n):
        claim = svc.ingest(
            text=f"Build server number {i} uses the shared artifact cache layout",
            citations=[CitationInput(source="session://chat", locator=f"turn-{i}", excerpt="cache layout")],
            subject=f"build-server-{i}",
            predicate="cache_layout",
            object_value="shared",
        )
        ids.append(claim.id)
    return ids


def _drift_events(store: SQLiteStore) -> list[dict[str, object]]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE event_type = 'system' AND details = ?",
            (qdrant_reconcile.MARKER_DRIFT,),
        ).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


def test_clean_skip_without_qdrant_url(svc: MemoryService) -> None:
    """With QDRANT_URL unset the phase must skip cleanly — no error, no event.

    Intent: spec §2.7 — most dev machines have no Qdrant; service.qdrant is
    None when QDRANT_URL is unset (service.py _init_qdrant) and the phase
    must be invisible there, not an 'error' that pollutes every cycle result
    and trains operators to ignore the key.
    """
    assert svc.qdrant is None, "fixture must model the no-QDRANT_URL machine"
    res = qdrant_reconcile.run(svc.store, svc.qdrant)
    assert res == {"skipped": "no_qdrant"}
    assert _drift_events(svc.store) == [], "a skip must not consume the daily throttle slot"


def test_drift_metric_computed_and_recorded(svc: MemoryService) -> None:
    """Drift = |sqlite truth - qdrant points| and lands as a qdrant_drift event.

    Intent: F11's failure is *invisibility* — the index drifts and nobody
    knows. The metric (both counts, recorded as a system event) is the §2.10
    dashboard/tripwire input; an in-sync backend must NOT trigger a sync_all
    (a needless full re-embed of the production corpus).
    """
    ids = _ingest_claims(svc, 3)
    fake = FakeQdrant(count=len(ids))
    res = qdrant_reconcile.run(svc.store, fake)
    assert res["sqlite_count"] == 3
    assert res["qdrant_count"] == 3
    assert res["drift"] == 0
    assert res["synced"] is False
    assert fake.sync_all_calls == 0
    events = _drift_events(svc.store)
    assert len(events) == 1
    assert events[0]["sqlite_count"] == 3
    assert events[0]["qdrant_count"] == 3


def test_threshold_respected_below_no_sync(svc: MemoryService) -> None:
    """Drift at-or-under the threshold must report but NOT sync.

    Intent: spec §2.7.2 — sync_all re-embeds every eligible claim through
    Ollama (minutes of GPU work on the live corpus). Small drift is normal
    churn between cycles; only a breach justifies the full convergence pass.
    """
    _ingest_claims(svc, 3)
    fake = FakeQdrant(count=1)  # drift = 2
    res = qdrant_reconcile.run(svc.store, fake, threshold=2)
    assert res["drift"] == 2
    assert res["synced"] is False
    assert fake.sync_all_calls == 0
    assert fake.deleted == []


def test_sync_all_and_orphan_delete_on_breach(svc: MemoryService) -> None:
    """Drift over the threshold must run sync_all AND delete orphan points.

    Intent: spec §2.7.2 — convergence has two halves. sync_all only upserts;
    points whose claim is archived/missing in SQLite would otherwise keep
    matching searches forever (ghost memories). Live claims' points must
    survive the delete pass.
    """
    ids = _ingest_claims(svc, 3)
    ghost_id = 999_999
    fake = FakeQdrant(count=50, point_claim_ids=[*ids, ghost_id])
    res = qdrant_reconcile.run(svc.store, fake, threshold=10)
    assert res["drift"] == 47
    assert res["synced"] is True
    assert fake.sync_all_calls == 1
    assert fake.deleted == [ghost_id], "only the ghost point dies; live claims keep their points"
    assert res["deleted"] == 1
    assert res["upserted"] == 5  # from FakeQdrant.sync_all stats
    events = _drift_events(svc.store)
    assert events[-1]["synced"] is True


def test_env_threshold_default_and_override(svc: MemoryService, monkeypatch: pytest.MonkeyPatch) -> None:
    """Threshold comes from MEMORYMASTER_QDRANT_DRIFT_MAX, defaulting to 100.

    Intent: spec §2.7.2 names this exact env var; an operator tuning it on
    the live fleet must actually change the breach point without a deploy.
    """
    assert qdrant_reconcile.drift_threshold() == 100
    monkeypatch.setenv(qdrant_reconcile.ENV_DRIFT_MAX, "5")
    assert qdrant_reconcile.drift_threshold() == 5
    _ingest_claims(svc, 3)
    fake = FakeQdrant(count=50)  # drift 47 > 5
    res = qdrant_reconcile.run(svc.store, fake)
    assert res["threshold"] == 5
    assert res["synced"] is True


def test_full_forces_sync_below_threshold(svc: MemoryService) -> None:
    """`full=True` (CLI --full) must sync even with zero drift.

    Intent: counts can match while contents are stale (same cardinality,
    wrong payloads/vectors). --full is the operator's recovery hammer after
    e.g. an embedding-model change; it must not be gated by the metric.
    """
    ids = _ingest_claims(svc, 2)
    fake = FakeQdrant(count=len(ids), point_claim_ids=list(ids))
    res = qdrant_reconcile.run(svc.store, fake, full=True)
    assert res["drift"] == 0
    assert res["synced"] is True
    assert fake.sync_all_calls == 1


def test_daily_throttle(svc: MemoryService) -> None:
    """The phase runs at most once per 24 h unless forced.

    Intent: spec §2.7 — reconcile is wired into every 6 h run_cycle, but
    counting + scrolling the production index is not free; the daily cadence
    mirrors integrity's quick_check throttle. `force=True` is the operator
    CLI path and must always run.
    """
    fake = FakeQdrant(count=0)
    first = qdrant_reconcile.run(svc.store, fake)
    assert "drift" in first
    second = qdrant_reconcile.run(svc.store, fake)
    assert second == {"skipped": "throttled"}
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    third = qdrant_reconcile.run(svc.store, fake, now=later)
    assert "drift" in third
    forced = qdrant_reconcile.run(svc.store, fake, force=True)
    assert "drift" in forced


def test_unreachable_qdrant_skips_without_consuming_throttle(svc: MemoryService) -> None:
    """count_points()=None (backend down) → skip, and the next run is still due.

    Intent: a transient Qdrant outage must not burn the daily slot — else a
    blip at cycle time defers reconciliation a full extra day, exactly the
    silent-drift window this job exists to close.
    """
    class DownQdrant(FakeQdrant):
        def count_points(self) -> int | None:
            return None

    res = qdrant_reconcile.run(svc.store, DownQdrant())
    assert res == {"skipped": "qdrant_unavailable"}
    assert _drift_events(svc.store) == []
    healthy = qdrant_reconcile.run(svc.store, FakeQdrant(count=0))
    assert "drift" in healthy, "outage must not throttle the next healthy attempt"


def test_run_cycle_includes_reconcile_phase(svc: MemoryService) -> None:
    """run_cycle must execute the phase and report it under 'qdrant_reconcile'.

    Intent: spec §2.7 wires reconcile as a steward phase precisely so it
    needs no new resident process — if run_cycle stops reporting the key,
    the discipline silently stopped running fleet-wide. Both shapes are
    pinned: clean skip without a backend, real metric with one.
    """
    result = svc.run_cycle()
    assert result["qdrant_reconcile"] == {"skipped": "no_qdrant"}

    fake = FakeQdrant(count=0)
    svc.qdrant = fake
    result2 = svc.run_cycle()
    phase = result2["qdrant_reconcile"]
    assert "drift" in phase
    assert phase["sqlite_count"] == 0


def test_cli_qdrant_reconcile_json(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """`memorymaster qdrant-reconcile --json` exists and reports the envelope.

    Intent: the operator runbook (and any scheduled wrapper) parses this
    JSON envelope; on a machine without Qdrant the command must exit 0 with
    the skip marker, not crash or return nonzero inside a cron.
    """
    from memorymaster.surfaces.cli import main

    monkeypatch.delenv("QDRANT_URL", raising=False)
    db = tmp_path / "cli-reconcile.db"
    SQLiteStore(db).init_db()
    rc = main(["--db", str(db), "--json", "qdrant-reconcile"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["data"] == {"skipped": "no_qdrant"}
