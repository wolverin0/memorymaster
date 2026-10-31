"""T-1870 AC3 -- decay ages a claim from its evidence, not from ``updated_at``.

``set_confidence`` moves ``updated_at`` on every real value change, and the cycle runs
deterministic/validator before decay. Aging from ``updated_at`` let a value change with
no new evidence restart the decay clock. Migration 32 adds ``last_evidence_at`` (ingest,
confirmation) and ``last_decayed_at`` (decay step); decay ages from the later of the two.
"""
from __future__ import annotations

import importlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.core.config import get_config
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern import llm_steward
from memorymaster.govern.jobs import decay, deterministic

migration = importlib.import_module("memorymaster.stores.migrations.0032_claim_decay_clock")


def _days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


@pytest.fixture()
def svc(tmp_path: Path) -> MemoryService:
    service = MemoryService(tmp_path / "decay-clock.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _set(svc: MemoryService, claim_id: int, **fields: object) -> None:
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with svc.store.connect() as conn:
        conn.execute(f"UPDATE claims SET {assignments} WHERE id = ?", [*fields.values(), claim_id])
        conn.commit()


def _confirmed(svc: MemoryService, key: str, *, evidence_days_ago: float, confidence: float = 0.9):
    # predicate "path" + an object absent from an empty workspace: deterministic lowers it.
    claim = svc.ingest(
        text=f"decay clock claim {key}", citations=[CitationInput(source=f"decay-clock-{key}")],
        idempotency_key=key, subject=f"service-{key}", predicate="path",
        object_value=f"missing-{key}-marker.cfg", source_agent="test-agent",
    )
    transition_claim(svc.store, claim.id, "confirmed", reason="fixture", event_type="validator")
    old = _days_ago(evidence_days_ago)
    _set(svc, claim.id, confidence=confidence, updated_at=old, last_evidence_at=old, last_decayed_at=None)
    return svc.store.get_claim(claim.id, include_citations=False)


def test_ingest_stamps_evidence(svc: MemoryService) -> None:
    claim = svc.ingest(text="fresh evidence", citations=[CitationInput(source="decay-clock")])
    assert claim.last_evidence_at == claim.created_at
    assert claim.last_decayed_at is None


def test_deterministic_value_change_does_not_restart_decay(svc: MemoryService, tmp_path: Path) -> None:
    claim = _confirmed(svc, "det", evidence_days_ago=10)
    workspace = tmp_path / "empty-workspace"  # not the DB folder: the DB itself contains the object
    workspace.mkdir()

    deterministic.run(svc.store, workspace_root=workspace, limit=50)
    after_det = svc.store.get_claim(claim.id, include_citations=False)
    assert after_det.confidence != claim.confidence  # a real value change ...
    assert after_det.updated_at != claim.updated_at  # ... which moves updated_at ...
    assert after_det.last_evidence_at == claim.last_evidence_at  # ... but not the decay clock

    decay.run(svc.store)

    rate = get_config().decay_rates["medium"]
    decayed = svc.store.get_claim(claim.id, include_citations=False)
    expected = max(0.0, after_det.confidence - rate * 10)
    assert decayed.confidence == pytest.approx(expected, abs=1e-3)
    assert decayed.last_decayed_at is not None


def test_consecutive_decay_runs_do_not_compound(svc: MemoryService) -> None:
    claim = _confirmed(svc, "twice", evidence_days_ago=5)

    decay.run(svc.store)
    first = svc.store.get_claim(claim.id, include_citations=False)
    decay.run(svc.store)
    second = svc.store.get_claim(claim.id, include_citations=False)

    rate = get_config().decay_rates["medium"]
    assert first.confidence == pytest.approx(0.9 - rate * 5, abs=1e-3)
    # The second run charges only the instant since the first, not the 5 days again.
    assert second.confidence == pytest.approx(first.confidence, abs=1e-4)


def test_confirmation_is_evidence(svc: MemoryService) -> None:
    claim = _confirmed(svc, "reconfirm", evidence_days_ago=60)
    transition_claim(svc.store, claim.id, "stale", reason="fixture", event_type="decay")
    _set(svc, claim.id, last_decayed_at=_days_ago(30))

    transition_claim(svc.store, claim.id, "confirmed", reason="revalidated", event_type="validator")
    current = svc.store.get_claim(claim.id, include_citations=False)
    assert _parse(current.last_evidence_at) > _parse(_days_ago(1))

    decay.run(svc.store)
    assert svc.store.get_claim(claim.id).confidence == pytest.approx(current.confidence, abs=1e-3)


def test_non_confirming_transition_keeps_evidence(svc: MemoryService) -> None:
    claim = _confirmed(svc, "staled", evidence_days_ago=20)
    transition_claim(svc.store, claim.id, "stale", reason="fixture", event_type="decay")
    assert svc.store.get_claim(claim.id).last_evidence_at == claim.last_evidence_at


def test_find_for_decay_orders_by_decay_clock(svc: MemoryService) -> None:
    old_evidence = _confirmed(svc, "old", evidence_days_ago=40)
    newer = _confirmed(svc, "newer", evidence_days_ago=5)
    # Maintenance freshened the old claim's updated_at; it must not lose its place in line.
    _set(svc, old_evidence.id, updated_at=_days_ago(0))
    _set(svc, newer.id, updated_at=_days_ago(90))

    assert [c.id for c in svc.store.find_for_decay(limit=1)] == [old_evidence.id]


def test_rescope_copy_adopts_the_decay_clock(svc: MemoryService) -> None:
    source = _confirmed(svc, "src", evidence_days_ago=12)
    _set(svc, source.id, last_decayed_at=_days_ago(3))
    copy = svc.ingest(text="rescoped copy", citations=[CitationInput(source="decay-clock-copy")])

    svc.store.adopt_claim_history(copy.id, source.id)

    adopted = svc.store.get_claim(copy.id)
    reloaded = svc.store.get_claim(source.id)
    assert (adopted.last_evidence_at, adopted.last_decayed_at) == (reloaded.last_evidence_at, reloaded.last_decayed_at)


def test_migration_backfills_from_citations_and_validation(svc: MemoryService) -> None:
    confirmed = _confirmed(svc, "mig-confirmed", evidence_days_ago=1)
    uncited = _confirmed(svc, "mig-uncited", evidence_days_ago=1)
    candidate = svc.ingest(text="still a candidate", citations=[CitationInput(source="decay-clock-cand")])
    created, cited, validated, updated = _days_ago(50), _days_ago(30), _days_ago(20), _days_ago(2)
    with svc.store.connect() as conn:
        # Rows as they were before migration 32: no clock.
        conn.execute("UPDATE claims SET last_evidence_at = NULL, last_decayed_at = NULL, created_at = ?", (created,))
        conn.execute("UPDATE claims SET last_validated_at = ?, updated_at = ? WHERE status = 'confirmed'",
                     (validated, updated))
        # A later stale/conflicted transition also writes last_validated_at: not evidence for a candidate.
        conn.execute("UPDATE claims SET last_validated_at = ? WHERE id = ?", (_days_ago(1), candidate.id))
        conn.execute("UPDATE citations SET created_at = ?", (cited,))
        conn.execute("DELETE FROM citations WHERE claim_id = ?", (uncited.id,))
        conn.execute("UPDATE claims SET last_validated_at = NULL WHERE id = ?", (uncited.id,))
        conn.commit()

        migration.apply_sqlite(conn)
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute("SELECT id, last_evidence_at, last_decayed_at FROM claims")
        }
        migration.apply_sqlite(conn)  # safe to retry: nothing changes
        assert rows == {
            r[0]: (r[1], r[2])
            for r in conn.execute("SELECT id, last_evidence_at, last_decayed_at FROM claims")
        }

    assert rows[confirmed.id] == (validated, updated)  # latest of created/cited/validated; decayed = updated_at
    assert rows[uncited.id] == (created, updated)  # NULL citation and NULL validation do not null the max
    assert rows[candidate.id] == (cited, None)  # candidates do not decay; stale-time validation ignored


def test_llm_steward_confirmation_restarts_the_decay_clock(svc: MemoryService) -> None:
    claim = svc.ingest(text="steward candidate", citations=[CitationInput(source="decay-clock-steward")],
                       source_agent="claude-session")
    old = _days_ago(40)
    _set(svc, claim.id, last_evidence_at=old)
    conn = sqlite3.connect(svc.store.db_path)
    try:
        assert llm_steward._confirm_candidate_cas(
            conn, claim_id=claim.id, version=svc.store.get_claim(claim.id).version,
            subject="s", predicate="p", object_value=None, confidence=0.9,
        )
        conn.commit()
    finally:
        conn.close()
    assert _parse(svc.store.get_claim(claim.id).last_evidence_at) > _parse(old)


def test_unpin_resumes_decay_instead_of_charging_the_pinned_period(svc: MemoryService) -> None:
    claim = _confirmed(svc, "pinned", evidence_days_ago=200)
    svc.store.set_pinned(claim.id, True, reason="fixture")
    assert claim.id not in [c.id for c in svc.store.find_for_decay()]

    svc.store.set_pinned(claim.id, False, reason="fixture")
    decay.run(svc.store)

    assert svc.store.get_claim(claim.id).confidence == pytest.approx(0.9, abs=1e-3)


def test_postgres_migration_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="SQLite-only"):
        migration.apply_postgres(object())
