from pathlib import Path

from memorymaster.bridges.db_merge import merge_databases
from test_db_merge_coverage_v2 import _claims, _init_db, _insert_claim, _snapshot


T10 = "2026-05-11T12:00:10+00:00"
T20 = "2026-05-11T12:00:20+00:00"


def _claim_by_key(path: Path, key: str):
    return {row["idempotency_key"]: row for row in _claims(path)}[key]


def test_later_updated_at_wins(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    _insert_claim(
        target,
        text="Claim X",
        idempotency_key="claim-x",
        confidence=0.8,
        updated_at=T10,
    )
    _insert_claim(
        source,
        text="Claim X",
        idempotency_key="claim-x",
        confidence=0.7,
        updated_at=T20,
    )

    merge_databases(str(target), str(source))

    row = _claim_by_key(target, "claim-x")
    assert row["confidence"] == 0.7
    assert row["updated_at"] == T20


def test_same_updated_at_deterministic(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    _insert_claim(source, text="Claim X", idempotency_key="claim-x", confidence=0.7, updated_at=T20)
    _insert_claim(target, text="Unrelated target claim", idempotency_key="target-dummy")
    _insert_claim(target, text="Claim X", idempotency_key="claim-x", confidence=0.8, updated_at=T20)

    merge_databases(str(target), str(source))

    # Ties on updated_at resolve to the lower claim id so repeated merges are stable.
    row = _claim_by_key(target, "claim-x")
    assert row["confidence"] == 0.7
    assert row["updated_at"] == T20


def test_idempotent_merge(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    _insert_claim(target, text="Claim X", idempotency_key="claim-x", confidence=0.8, updated_at=T10)
    _insert_claim(source, text="Claim X", idempotency_key="claim-x", confidence=0.7, updated_at=T20)

    merge_databases(str(target), str(source))
    after_first = _snapshot(target)
    merge_databases(str(target), str(source))

    assert _snapshot(target) == after_first
