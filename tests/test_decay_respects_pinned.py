from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.govern.jobs import decay
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.stores.storage import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    db_path = tmp_path / "decay_respects_pinned.db"
    sqlite_store = SQLiteStore(db_path)
    sqlite_store.init_db()
    return sqlite_store


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _create_claim(
    store: SQLiteStore,
    *,
    text: str,
    status: str = "confirmed",
    pinned: bool = False,
    confidence: float = 0.9,
    days_since_validation: int = 365,
):
    claim = store.create_claim(
        text=text,
        citations=[CitationInput(source="decay-pinned-test", locator=text)],
        subject=text,
        predicate="decay_status",
        object_value=text,
        volatility="high",
    )
    if status == "confirmed":
        transition_claim(store, claim.id, to_status="confirmed", reason="test setup", event_type="transition")
    elif status != "candidate":
        raise ValueError(f"unsupported test status: {status}")
    store.set_confidence(claim.id, confidence, details="test setup")
    if pinned:
        store.set_pinned(claim.id, True, reason="test setup")
    _set_claim_fields(
        store,
        claim.id,
        confidence=confidence,
        updated_at=_iso_days_ago(days_since_validation),
        last_validated_at=_iso_days_ago(days_since_validation),
    )
    return store.get_claim(claim.id, include_citations=False)


def _set_claim_fields(store: SQLiteStore, claim_id: int, **fields: object) -> None:
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values())
    with store.connect() as conn:
        conn.execute(f"UPDATE claims SET {assignments} WHERE id = ?", [*values, claim_id])


@pytest.mark.parametrize("initial_status", ["confirmed", "candidate"])
def test_pinned_claim_never_decays(store: SQLiteStore, initial_status: str) -> None:
    claim = _create_claim(
        store,
        text=f"pinned old {initial_status} claim",
        status=initial_status,
        pinned=True,
    )

    result = decay.run(store)

    current = store.get_claim(claim.id, include_citations=False)
    assert result == {"processed": 0, "decayed": 0, "to_stale": 0}
    assert current is not None
    assert current.status == initial_status
    assert current.status != "stale"
    assert current.confidence == pytest.approx(claim.confidence)


def test_unpinned_claim_does_decay(store: SQLiteStore) -> None:
    claim = _create_claim(
        store,
        text="unpinned old confirmed claim",
        pinned=False,
    )

    result = decay.run(store)

    current = store.get_claim(claim.id, include_citations=False)
    assert result == {"processed": 1, "decayed": 1, "to_stale": 1}
    assert current is not None
    assert current.status == "stale"
    assert current.confidence < claim.confidence


def test_pin_after_decay_does_NOT_unstale(store: SQLiteStore) -> None:
    claim = _create_claim(
        store,
        text="unpinned claim that decays before being pinned",
        pinned=False,
    )
    decay.run(store)
    stale_claim = store.get_claim(claim.id, include_citations=False)
    assert stale_claim is not None
    assert stale_claim.status == "stale"

    store.set_pinned(claim.id, True, reason="test setup after stale")
    result = decay.run(store)

    current = store.get_claim(claim.id, include_citations=False)
    assert result == {"processed": 0, "decayed": 0, "to_stale": 0}
    assert current is not None
    assert current.pinned is True
    assert current.status == "stale"
