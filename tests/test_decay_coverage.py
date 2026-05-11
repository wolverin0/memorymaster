from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.jobs import decay
from memorymaster.lifecycle import transition_claim
from memorymaster.models import CitationInput
from memorymaster.storage import SQLiteStore


DECAY_COVERAGE_REGRESSION_SENTINEL = "decay-coverage-regression-sentinel"


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    db_path = tmp_path / "decay_coverage.db"
    sqlite_store = SQLiteStore(db_path)
    sqlite_store.init_db()
    return sqlite_store


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _create_confirmed_claim(
    store: SQLiteStore,
    *,
    text: str,
    confidence: float = 0.9,
    updated_days_ago: float = 1.0,
    volatility: str = "medium",
    pinned: bool = False,
):
    claim = store.create_claim(
        text=text,
        citations=[CitationInput(source="decay-test", locator=DECAY_COVERAGE_REGRESSION_SENTINEL)],
        subject=text,
        predicate="decays_as",
        object_value=text,
        volatility=volatility,
    )
    transition_claim(store, claim.id, to_status="confirmed", reason="test setup", event_type="transition")
    store.set_confidence(claim.id, confidence, details="test setup")
    if pinned:
        store.set_pinned(claim.id, True, reason="test setup")
    _set_claim_fields(
        store,
        claim.id,
        updated_at=_iso_days_ago(updated_days_ago),
        confidence=confidence,
    )
    return store.get_claim(claim.id, include_citations=False)


def _set_claim_fields(store: SQLiteStore, claim_id: int, **fields: object) -> None:
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values())
    with store.connect() as conn:
        conn.execute(f"UPDATE claims SET {assignments} WHERE id = ?", [*values, claim_id])


def test_empty_store_runs_without_error(store: SQLiteStore) -> None:
    result = decay.run(store)

    assert result == {"processed": 0, "decayed": 0, "to_stale": 0}


def test_fresh_confirmed_claims_decay_but_do_not_transition(store: SQLiteStore) -> None:
    claim = _create_confirmed_claim(
        store,
        text="fresh high-confidence claim",
        confidence=0.95,
        updated_days_ago=0.01,
        volatility="high",
    )

    result = decay.run(store)

    current = store.get_claim(claim.id, include_citations=False)
    assert result == {"processed": 1, "decayed": 1, "to_stale": 0}
    assert current is not None
    assert current.status == "confirmed"
    assert current.confidence < 0.95
    assert current.confidence > 0.35


def test_all_stale_eligible_claims_transition_to_stale(store: SQLiteStore) -> None:
    claims = [
        _create_confirmed_claim(
            store,
            text=f"old high-volatility claim {index}",
            confidence=0.9,
            updated_days_ago=30,
            volatility="high",
        )
        for index in range(3)
    ]

    result = decay.run(store)

    assert result == {"processed": 3, "decayed": 3, "to_stale": 3}
    statuses = [store.get_claim(claim.id, include_citations=False).status for claim in claims]
    assert statuses == ["stale", "stale", "stale"]


def test_pinned_confirmed_claim_is_skipped_by_decay_selection(store: SQLiteStore) -> None:
    claim = _create_confirmed_claim(
        store,
        text="pinned old claim",
        confidence=0.1,
        updated_days_ago=365,
        volatility="high",
        pinned=True,
    )

    result = decay.run(store)

    current = store.get_claim(claim.id, include_citations=False)
    assert result == {"processed": 0, "decayed": 0, "to_stale": 0}
    assert current is not None
    assert current.status == "confirmed"
    assert current.confidence == pytest.approx(0.1)
    assert current.pinned is True


def test_mixed_claims_only_transition_below_threshold(store: SQLiteStore) -> None:
    stale = _create_confirmed_claim(
        store,
        text="mixed old claim",
        confidence=0.7,
        updated_days_ago=20,
        volatility="high",
    )
    fresh = _create_confirmed_claim(
        store,
        text="mixed fresh claim",
        confidence=0.95,
        updated_days_ago=0.5,
        volatility="low",
    )

    result = decay.run(store)

    stale_current = store.get_claim(stale.id, include_citations=False)
    fresh_current = store.get_claim(fresh.id, include_citations=False)
    assert result == {"processed": 2, "decayed": 2, "to_stale": 1}
    assert stale_current is not None
    assert stale_current.status == "stale"
    assert fresh_current is not None
    assert fresh_current.status == "confirmed"


def test_already_stale_claim_is_not_processed_again(store: SQLiteStore) -> None:
    claim = _create_confirmed_claim(
        store,
        text="already stale claim",
        confidence=0.1,
        updated_days_ago=90,
        volatility="high",
    )
    transition_claim(store, claim.id, to_status="stale", reason="pre-existing stale", event_type="decay")
    _set_claim_fields(store, claim.id, updated_at=_iso_days_ago(90), confidence=0.1)

    result = decay.run(store)

    current = store.get_claim(claim.id, include_citations=False)
    assert result == {"processed": 0, "decayed": 0, "to_stale": 0}
    assert current is not None
    assert current.status == "stale"
    assert current.confidence == pytest.approx(0.1)


@pytest.mark.parametrize("terminal_status", ["superseded", "archived"])
def test_terminal_status_claims_are_skipped(store: SQLiteStore, terminal_status: str) -> None:
    claim = _create_confirmed_claim(
        store,
        text=f"{terminal_status} terminal claim",
        confidence=0.1,
        updated_days_ago=90,
        volatility="high",
    )
    _set_claim_fields(store, claim.id, status=terminal_status, updated_at=_iso_days_ago(90), confidence=0.1)

    result = decay.run(store)

    current = store.get_claim(claim.id, include_citations=False)
    assert result == {"processed": 0, "decayed": 0, "to_stale": 0}
    assert current is not None
    assert current.status == terminal_status
    assert current.confidence == pytest.approx(0.1)


def test_custom_stale_threshold_controls_transition(store: SQLiteStore) -> None:
    claim = _create_confirmed_claim(
        store,
        text="custom threshold claim",
        confidence=0.55,
        updated_days_ago=10,
        volatility="medium",
    )

    result = decay.run(store, stale_threshold=0.36)

    current = store.get_claim(claim.id, include_citations=False)
    assert result == {"processed": 1, "decayed": 1, "to_stale": 1}
    assert current is not None
    assert current.status == "stale"
    assert current.confidence < 0.36
