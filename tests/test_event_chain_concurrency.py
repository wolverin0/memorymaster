"""T-1865 / F13: record_event must read the hash-chain head inside the write transaction.

Before the fix the head SELECT ran on an autocommit connection and the INSERT opened
its transaction afterwards, so two writers could both extend the same predecessor
(``broken_prev_link`` in ``reconcile_integrity``).
"""
from __future__ import annotations

import sqlite3
import threading
import time

from memorymaster.stores import _storage_schema
from memorymaster.stores._storage_schema import _SchemaMixin
from memorymaster.stores.storage import SQLiteStore


def _chain_issues(store: SQLiteStore) -> list[dict]:
    report = store.reconcile_integrity(limit=1000)
    return report["issues"]["hash_chain_issues"]


def _hammer(store: SQLiteStore, threads: int, per_thread: int) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(threads)

    def work(n: int) -> None:
        try:
            barrier.wait()
            for i in range(per_thread):
                store.record_event(
                    claim_id=None, event_type="audit", details=f"writer-{n}-{i}",
                )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
            errors.append(exc)

    pool = [threading.Thread(target=work, args=(n,)) for n in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    return errors


def test_concurrent_record_event_keeps_the_chain_linear(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "chain.db")
    store.init_db()

    real = _SchemaMixin._compute_event_hash

    def slow(**kwargs):
        time.sleep(0.01)  # widen the head-read -> insert window so the race is deterministic
        return real(**kwargs)

    monkeypatch.setattr(_storage_schema._SchemaMixin, "_compute_event_hash", staticmethod(slow))

    errors = _hammer(store, threads=4, per_thread=15)

    assert errors == []
    issues = _chain_issues(store)
    assert [i for i in issues if i["reason"] == "broken_prev_link"] == []
    assert issues == []
    with sqlite3.connect(tmp_path / "chain.db") as conn:
        heads = conn.execute(
            "SELECT prev_event_hash, COUNT(*) FROM events WHERE event_hash IS NOT NULL "
            "GROUP BY prev_event_hash HAVING COUNT(*) > 1"
        ).fetchall()
    assert heads == []  # no two events share a predecessor


def test_record_event_respects_a_caller_owned_transaction(tmp_path):
    store = SQLiteStore(tmp_path / "owned.db")
    store.init_db()
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        first = store._insert_event_row(
            conn, claim_id=None, event_type="audit", from_status=None, to_status=None,
            details="first", payload_json=None, created_at="2026-01-01T00:00:00+00:00",
        )
        second = store._insert_event_row(
            conn, claim_id=None, event_type="audit", from_status=None, to_status=None,
            details="second", payload_json=None, created_at="2026-01-01T00:00:01+00:00",
        )
        assert conn.in_transaction  # the helper never committed or restarted the caller's txn
        conn.rollback()
    assert first < second
    with sqlite3.connect(tmp_path / "owned.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE details IN ('first','second')").fetchone()[0] == 0
