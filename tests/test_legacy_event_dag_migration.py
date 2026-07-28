"""Verify the SQLite legacy event-DAG compatibility migration."""
from __future__ import annotations

import importlib
import sqlite3

import pytest


def _migration(version: str):
    return importlib.import_module(f"memorymaster.stores.migrations.{version}")


def _event_hash(
    v10,
    *,
    event_id: int,
    previous: str | None,
    event_type: str = "ingest",
) -> str:
    row = (
        event_id,
        1,
        event_type,
        None,
        None,
        "",
        None,
        f"2026-01-01T00:00:0{event_id}+00:00",
        previous,
        None,
        "sha256-v1",
        None,
        None,
        None,
        None,
    )
    return v10._compute_primary_event_hash(
        row,
        hash_algo="sha256-v1",
        previous=previous,
        normalize_utc=False,
    )


def _legacy_dag_connection(v10):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE claims (id INTEGER PRIMARY KEY, tenant_id TEXT);
        INSERT INTO claims VALUES (1, 'tenant-a');
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            claim_id INTEGER,
            event_type TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            details TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            prev_event_hash TEXT,
            event_hash TEXT,
            hash_algo TEXT
        );
        """
    )
    first = _event_hash(v10, event_id=1, previous=None)
    second = _event_hash(v10, event_id=2, previous=first)
    fork = _event_hash(v10, event_id=3, previous=first, event_type="extractor")
    missing = "f" * 64
    gap = _event_hash(v10, event_id=4, previous=missing)
    rows = (
        (1, 1, "ingest", first, None),
        (2, 1, "ingest", second, first),
        (3, 1, "extractor", fork, first),
        (4, 1, "ingest", gap, missing),
    )
    conn.executemany(
        """
        INSERT INTO events(
            id, claim_id, event_type, details, created_at,
            event_hash, prev_event_hash, hash_algo
        ) VALUES (?, ?, ?, '', '2026-01-01T00:00:0' || ? || '+00:00',
                  ?, ?, 'sha256-v1')
        """,
        (
            (event_id, claim_id, event_type, event_id, event_hash, previous)
            for event_id, claim_id, event_type, event_hash, previous in rows
        ),
    )
    conn.commit()
    return conn, rows


def _assert_preserved_primary_rows(conn, rows) -> None:
    preserved = conn.execute(
        "SELECT id, prev_event_hash, event_hash FROM events ORDER BY id"
    ).fetchall()
    assert [(row[0], row[1], row[2]) for row in preserved] == [
        (event_id, previous, event_hash)
        for event_id, _claim_id, _type, event_hash, previous in rows
    ]


def _assert_tenant_chain_and_guards(conn) -> None:
    tenant_rows = conn.execute(
        """
        SELECT tenant_id, tenant_prev_event_hash, tenant_event_hash,
               tenant_hash_algo
        FROM events ORDER BY id
        """
    ).fetchall()
    assert all(row[0] == "tenant-a" for row in tenant_rows)
    assert all(row[2] and row[3] == "sha256-tenant-v2" for row in tenant_rows)
    assert tenant_rows[0][1] is None
    assert tenant_rows[1][1] == tenant_rows[0][2]
    triggers = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    assert {
        "trg_events_append_only_update",
        "trg_events_append_only_delete",
    }.issubset(triggers)


def test_preserves_primary_hashes_and_builds_tenant_chain() -> None:
    v10 = _migration("0010_tenant_event_ledger")
    v18 = _migration("0018_legacy_event_dag")
    conn, rows = _legacy_dag_connection(v10)

    v18.apply_sqlite(conn)

    _assert_preserved_primary_rows(conn, rows)
    _assert_tenant_chain_and_guards(conn)
    conn.close()


def test_rejects_self_valid_forward_primary_link() -> None:
    v10 = _migration("0010_tenant_event_ledger")
    v18 = _migration("0018_legacy_event_dag")
    second_hash = _event_hash(v10, event_id=2, previous=None)
    first_hash = _event_hash(v10, event_id=1, previous=second_hash)
    rows = [
        (
            1, 1, "ingest", None, None, "", None,
            "2026-01-01T00:00:01+00:00",
            second_hash, first_hash, "sha256-v1", None, None, None, None,
        ),
        (
            2, 1, "ingest", None, None, "", None,
            "2026-01-01T00:00:02+00:00",
            None, second_hash, "sha256-v1", None, None, None, None,
        ),
    ]

    with pytest.raises(RuntimeError, match="Forward primary event link"):
        v18._verify_primary_dag(rows)
