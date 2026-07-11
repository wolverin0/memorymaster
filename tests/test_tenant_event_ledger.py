"""Adversarial tests for tenant-aware event storage and PostgreSQL chains."""
from __future__ import annotations

import importlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.stores._storage_shared import (
    EVENT_HASH_ALGO,
    compute_tenant_event_hash,
)
from memorymaster.stores.migrations import discover_migrations
from memorymaster.stores.postgres_store import PostgresStore
from memorymaster.stores.storage import SQLiteStore


TENANT_EVENT_HASH_ALGO = "sha256-tenant-v2"


class RecordingCursor:
    def __init__(
        self,
        claim_tenant: str = "tenant-a",
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.claim_tenant = claim_tenant
        self.rows = rows or []
        self.executed: list[tuple[str, object]] = []
        self.executed_many: list[tuple[str, object]] = []
        self.last_sql = ""

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object = ()) -> None:
        self.last_sql = " ".join(sql.split())
        self.executed.append((self.last_sql, params))

    def executemany(self, sql: str, params: object) -> None:
        normalized = " ".join(sql.split())
        self.executed_many.append((normalized, params))

    def fetchone(self):
        if "SELECT tenant_id FROM claims" in self.last_sql:
            return {"tenant_id": self.claim_tenant}
        if "INSERT INTO events" in self.last_sql:
            return {"id": 42}
        return None

    def fetchall(self) -> list[object]:
        return list(self.rows)


class RecordingConnection:
    def __init__(
        self,
        claim_tenant: str = "tenant-a",
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.cursor_instance = RecordingCursor(claim_tenant, rows)
        self.commits = 0

    def __enter__(self) -> "RecordingConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1


class HistoricalHeadCursor(RecordingCursor):
    def fetchone(self):
        if "SELECT event_hash FROM events" in self.last_sql:
            return {"event_hash": "primary-v2-head"}
        if "SELECT tenant_event_hash FROM events" in self.last_sql:
            return {"tenant_event_hash": "tenant-history-head"}
        return super().fetchone()


class FailingTriggerConnection:
    """Inject a trigger recreation failure without hiding transaction state."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def execute(self, sql: str, params: object = ()):
        if "CREATE TRIGGER IF NOT EXISTS trg_events_append_only_delete" in sql:
            raise sqlite3.OperationalError("injected trigger recreation failure")
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, params: object):
        return self.conn.executemany(sql, params)

    def executescript(self, _sql: str):
        self.conn.commit()
        raise sqlite3.OperationalError("injected trigger recreation failure")

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()


def _migration():
    return importlib.import_module(
        "memorymaster.stores.migrations.0010_tenant_event_ledger"
    )


def _postgres_store(claim_tenant: str = "tenant-a") -> tuple[PostgresStore, RecordingConnection]:
    store = PostgresStore(
        "postgresql://db.invalid/app",
        tenant_id="tenant-a",
        require_tenant=True,
    )
    store._psycopg = (object(), object(), lambda value: value)
    return store, RecordingConnection(claim_tenant)


def test_migration_backfills_event_tenant_without_rewriting_hashes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE claims (id INTEGER PRIMARY KEY, tenant_id TEXT);
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
        INSERT INTO claims(id, tenant_id) VALUES
            (1, 'tenant-a'),
            (2, 'tenant-b'),
            (3, 'tenant-a');
        """
    )
    original_triples: list[tuple[str | None, str, str]] = []
    previous: str | None = None
    for event_id, claim_id in ((1, 1), (2, 2), (3, 3)):
        event_hash = SQLiteStore._compute_event_hash(
            claim_id=claim_id,
            event_type="ingest",
            from_status=None,
            to_status="candidate",
            details=f"fixture-{event_id}",
            payload_json=None,
            created_at="2026-07-10T00:00:00+00:00",
            prev_event_hash=previous,
        )
        conn.execute(
            """
            INSERT INTO events(
                id, claim_id, event_type, to_status, details, created_at,
                prev_event_hash, event_hash, hash_algo
            ) VALUES (?, ?, 'ingest', 'candidate', ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                claim_id,
                f"fixture-{event_id}",
                "2026-07-10T00:00:00+00:00",
                previous,
                event_hash,
                EVENT_HASH_ALGO,
            ),
        )
        original_triples.append((previous, event_hash, EVENT_HASH_ALGO))
        previous = event_hash
    conn.commit()
    try:
        _migration().apply_sqlite(conn)
        rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    finally:
        conn.close()

    assert [row["tenant_id"] for row in rows] == ["tenant-a", "tenant-b", "tenant-a"]
    assert [
        (row["prev_event_hash"], row["event_hash"], row["hash_algo"])
        for row in rows
    ] == original_triples
    assert rows[0]["tenant_prev_event_hash"] is None
    assert rows[1]["tenant_prev_event_hash"] is None
    assert rows[2]["tenant_prev_event_hash"] == rows[0]["tenant_event_hash"]
    assert {row["tenant_hash_algo"] for row in rows} == {TENANT_EVENT_HASH_ALGO}


def test_postgres_migration_is_versioned_and_preserves_hash_columns() -> None:
    migration = next(item for item in discover_migrations() if item.version == 10)
    conn = RecordingConnection()

    migration.apply_postgres(conn)
    emitted = "\n".join(sql for sql, _ in conn.cursor_instance.executed)

    assert "event" in migration.description.lower()
    assert "ALTER TABLE events ADD COLUMN IF NOT EXISTS tenant_id TEXT" in emitted
    assert "tenant_prev_event_hash" in emitted
    assert "tenant_event_hash" in emitted
    assert "tenant_hash_algo" in emitted
    assert "SET tenant_id = claim.tenant_id" in emitted
    assert "idx_events_tenant_id" in emitted
    assert "idx_events_tenant_head" in emitted
    assert "idx_events_tenant_algo_head" in emitted
    assert "CREATE POLICY memorymaster_tenant_restrict ON events" in emitted
    assert "LOCK TABLE events IN ACCESS EXCLUSIVE MODE" in emitted
    assert "LOCK TABLE events IN SHARE ROW EXCLUSIVE MODE" not in emitted
    assert "events.claim_id IS NULL OR EXISTS" in emitted
    assert "SET event_hash" not in emitted
    assert "SET prev_event_hash" not in emitted
    assert conn.commits == 1


def test_event_hash_algorithms_have_checksum_frozen_golden_vectors() -> None:
    kwargs = {
        "claim_id": 7,
        "event_type": "ingest",
        "from_status": None,
        "to_status": "candidate",
        "details": "fixture",
        "payload": {"count": 1},
        "created_at": datetime(2026, 7, 10, tzinfo=timezone.utc),
        "prev_event_hash": None,
    }
    v1_hash = PostgresStore._compute_event_hash(**kwargs, hash_algo=EVENT_HASH_ALGO)
    tenant_primary_hash = PostgresStore._compute_event_hash(
        **kwargs,
        tenant_id="tenant-a",
        hash_algo=TENANT_EVENT_HASH_ALGO,
    )
    tenant_chain_hash = compute_tenant_event_hash(
        tenant_id="tenant-a",
        event_hash=v1_hash,
        tenant_prev_event_hash=None,
    )

    assert v1_hash == "7e29764a32caa9370045ddeeaffb9684d596983c99a16c3f280ed696949ff286"
    assert tenant_primary_hash == "a49553710e1c35c1982a4e36dd70a8889c8b6987aa9ceeb237ed82e16721b1fb"
    assert tenant_chain_hash == "adfcc310faaa12a1429ad3007e627acbbe9c79085666c1442299195862f213cc"
    assert (
        _migration()._compute_tenant_event_hash_v2(
            tenant_id="tenant-a",
            event_hash=v1_hash,
            tenant_prev_event_hash=None,
        )
        == tenant_chain_hash
    )


def test_postgres_event_hashes_canonicalize_non_utc_timestamps() -> None:
    non_utc = datetime(
        2026,
        7,
        9,
        21,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    kwargs = {
        "claim_id": 7,
        "event_type": "ingest",
        "from_status": None,
        "to_status": "candidate",
        "details": "fixture",
        "payload": {"count": 1},
        "created_at": non_utc,
        "prev_event_hash": None,
    }

    assert (
        PostgresStore._compute_event_hash(**kwargs, hash_algo=EVENT_HASH_ALGO)
        == "7e29764a32caa9370045ddeeaffb9684d596983c99a16c3f280ed696949ff286"
    )
    assert (
        PostgresStore._compute_event_hash(
            **kwargs,
            tenant_id="tenant-a",
            hash_algo=TENANT_EVENT_HASH_ALGO,
        )
        == "a49553710e1c35c1982a4e36dd70a8889c8b6987aa9ceeb237ed82e16721b1fb"
    )


def test_migration_accepts_historical_v1_raw_offset_hash() -> None:
    row = {
        "id": 1,
        "claim_id": 7,
        "event_type": "ingest",
        "from_status": None,
        "to_status": "candidate",
        "details": "fixture",
        "payload_json": {"count": 1},
        "created_at": datetime(
            2026,
            7,
            9,
            21,
            tzinfo=timezone(timedelta(hours=-3)),
        ),
        "prev_event_hash": None,
        "event_hash": "3b8c92d37c952664fe8436dd2690cbbdcb8449ae91b02ee2f7a12b50a814b5ef",
        "hash_algo": EVENT_HASH_ALGO,
        "tenant_id": "tenant-a",
        "tenant_prev_event_hash": None,
        "tenant_event_hash": None,
        "tenant_hash_algo": None,
    }

    _migration()._validate_primary_chain([row])
    assert PostgresStore._event_chain_issues([row], limit=20) == []

    canonical_row = dict(row)
    canonical_row["event_hash"] = (
        "7e29764a32caa9370045ddeeaffb9684d596983c99a16c3f280ed696949ff286"
    )
    assert PostgresStore._event_chain_issues([canonical_row], limit=20) == []


def test_legacy_schema_ensure_does_not_preapply_tenant_event_migration() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE claims (id INTEGER PRIMARY KEY);
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            claim_id INTEGER,
            event_type TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            details TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    try:
        SQLiteStore._ensure_event_integrity_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    finally:
        conn.close()

    assert {"prev_event_hash", "event_hash", "hash_algo"} <= columns
    assert "tenant_id" not in columns
    assert "tenant_event_hash" not in columns


def test_read_only_reconciliation_does_not_run_schema_repairs(tmp_path, monkeypatch) -> None:
    service = MemoryService(tmp_path / "read-only-reconcile.db", workspace_root=tmp_path)
    service.init_db()

    def fail_if_called(_conn) -> None:
        pytest.fail("read-only reconciliation invoked a schema repair helper")

    monkeypatch.setattr(service.store, "_ensure_event_integrity_schema", fail_if_called)
    report = service.store.reconcile_integrity(fix=False)

    assert report["summary"]["hash_chain_issues"] == 0


def test_sqlite_reconciliation_detects_v1_payload_tampering(tmp_path) -> None:
    service = MemoryService(tmp_path / "tampered-v1.db", workspace_root=tmp_path)
    service.init_db()
    claim = service.ingest(
        text="SQLite integrity fixture.",
        citations=[CitationInput(source="test")],
        source_agent="tenant-event-test",
    )
    with service.store.connect() as conn:
        conn.execute("DROP TRIGGER trg_events_append_only_update")
        conn.execute(
            "UPDATE events SET details = 'tampered' WHERE claim_id = ?",
            (claim.id,),
        )
        conn.commit()

    report = service.store.reconcile_integrity(fix=False)

    assert any(
        issue["reason"] == "event_hash_mismatch"
        for issue in report["issues"]["hash_chain_issues"]
    )


def test_sqlite_reconciliation_detects_tenant_chain_tampering(tmp_path) -> None:
    service = MemoryService(
        tmp_path / "tampered-tenant-chain.db",
        workspace_root=tmp_path,
        tenant_id="tenant-a",
    )
    service.init_db()
    claim = service.ingest(
        text="SQLite tenant chain fixture.",
        citations=[CitationInput(source="test")],
        source_agent="tenant-event-test",
    )
    with service.store.connect() as conn:
        conn.execute("DROP TRIGGER trg_events_append_only_update")
        conn.execute(
            "UPDATE events SET tenant_event_hash = 'tampered' WHERE claim_id = ?",
            (claim.id,),
        )
        conn.commit()

    report = service.store.reconcile_integrity(fix=False)

    assert report["summary"]["tenant_hash_chain_issues"] > 0


def test_legacy_sqlite_event_insert_falls_back_to_original_columns() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE claims (id INTEGER PRIMARY KEY);
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            claim_id INTEGER,
            event_type TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            details TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    try:
        event_id = SQLiteStore._insert_event_row(
            conn,
            claim_id=None,
            event_type="system",
            from_status=None,
            to_status=None,
            details="legacy fallback",
            payload_json=None,
            created_at="2026-07-10T00:00:00+00:00",
        )
        stored = conn.execute("SELECT event_type FROM events WHERE id = ?", (event_id,)).fetchone()
    finally:
        conn.close()

    assert stored["event_type"] == "system"


def test_sqlite_migration_rolls_back_if_trigger_recreation_fails() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE claims (id INTEGER PRIMARY KEY, tenant_id TEXT);
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
        CREATE TRIGGER trg_events_append_only_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'append only'); END;
        CREATE TRIGGER trg_events_append_only_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'append only'); END;
        """
    )
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            _migration().apply_sqlite(FailingTriggerConnection(conn))
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_events_append_only_%'"
            )
        }
    finally:
        conn.close()

    assert triggers == {"trg_events_append_only_update", "trg_events_append_only_delete"}


def test_migration_rejects_invalid_primary_chain_before_tenant_updates() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE claims (id INTEGER PRIMARY KEY, tenant_id TEXT);
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
        INSERT INTO claims(id, tenant_id) VALUES (1, 'tenant-a');
        INSERT INTO events VALUES (
            1, 1, 'ingest', NULL, 'candidate', 'bad chain', NULL,
            '2026-07-10T00:00:00+00:00', 'not-the-head', 'invalid-hash', 'sha256-v1'
        );
        """
    )
    try:
        with pytest.raises(RuntimeError, match="primary event chain"):
            _migration().apply_sqlite(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        tenant_values = (
            conn.execute("SELECT tenant_id FROM events").fetchall()
            if "tenant_id" in columns
            else []
        )
    finally:
        conn.close()

    assert not tenant_values or all(row[0] is None for row in tenant_values)


def test_migration_rejects_conflicting_existing_tenant_prefix() -> None:
    rows = [
        {
            "id": 1,
            "tenant_id": "tenant-a",
            "event_hash": "primary-1",
            "tenant_prev_event_hash": "wrong-head",
            "tenant_event_hash": "wrong-hash",
            "tenant_hash_algo": TENANT_EVENT_HASH_ALGO,
        }
    ]

    with pytest.raises(RuntimeError, match="tenant event chain"):
        _migration()._tenant_hash_updates(rows)


def test_migration_resumes_and_validates_partial_tenant_prefix() -> None:
    first_hash = _migration()._compute_tenant_event_hash_v2(
        tenant_id="tenant-a",
        event_hash="primary-1",
        tenant_prev_event_hash=None,
    )
    rows = [
        {
            "id": 1,
            "tenant_id": "tenant-a",
            "event_hash": "primary-1",
            "tenant_prev_event_hash": None,
            "tenant_event_hash": first_hash,
            "tenant_hash_algo": TENANT_EVENT_HASH_ALGO,
        },
        {
            "id": 2,
            "tenant_id": "tenant-a",
            "event_hash": "primary-2",
            "tenant_prev_event_hash": None,
            "tenant_event_hash": None,
            "tenant_hash_algo": None,
        },
    ]

    updates = _migration()._tenant_hash_updates(rows)

    assert len(updates) == 1
    assert updates[0][0] == first_hash
    rows[1]["tenant_prev_event_hash"] = updates[0][0]
    rows[1]["tenant_event_hash"] = updates[0][1]
    rows[1]["tenant_hash_algo"] = updates[0][2]
    assert _migration()._tenant_hash_updates(rows) == []


def test_postgres_migration_executes_backfill_and_complete_rerun_is_noop() -> None:
    created_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
    primary_hash = PostgresStore._compute_event_hash(
        claim_id=1,
        event_type="ingest",
        from_status=None,
        to_status="candidate",
        details="fixture",
        payload=None,
        created_at=created_at,
        prev_event_hash=None,
        hash_algo=EVENT_HASH_ALGO,
    )
    row = {
        "id": 1,
        "claim_id": 1,
        "event_type": "ingest",
        "from_status": None,
        "to_status": "candidate",
        "details": "fixture",
        "payload_json": None,
        "created_at": created_at,
        "prev_event_hash": None,
        "event_hash": primary_hash,
        "hash_algo": EVENT_HASH_ALGO,
        "tenant_id": "tenant-a",
        "tenant_prev_event_hash": None,
        "tenant_event_hash": None,
        "tenant_hash_algo": None,
    }
    conn = RecordingConnection(rows=[row])

    _migration().apply_postgres(conn)

    assert len(conn.cursor_instance.executed_many) == 1
    _, updates = conn.cursor_instance.executed_many[0]
    update = list(updates)[0]
    complete_row = dict(row)
    complete_row.update(
        tenant_prev_event_hash=update[0],
        tenant_event_hash=update[1],
        tenant_hash_algo=update[2],
    )
    rerun = RecordingConnection(rows=[complete_row])
    _migration().apply_postgres(rerun)
    assert rerun.cursor_instance.executed_many == []


def test_postgres_hash_repair_is_partition_aware_after_v2_cutover() -> None:
    created_at = datetime(2026, 7, 10, tzinfo=timezone.utc)

    def event_row(
        event_id: int,
        *,
        tenant_id: str | None,
        hash_algo: str,
        previous: str | None,
        event_hash: str | None,
    ) -> dict[str, object]:
        return {
            "id": event_id,
            "claim_id": event_id,
            "event_type": "ingest",
            "from_status": None,
            "to_status": "candidate",
            "details": f"fixture-{event_id}",
            "payload_json": None,
            "created_at": created_at,
            "prev_event_hash": previous,
            "event_hash": event_hash,
            "hash_algo": hash_algo,
            "tenant_id": tenant_id,
        }

    v1_head = PostgresStore._compute_event_hash(
        claim_id=1,
        event_type="ingest",
        from_status=None,
        to_status="candidate",
        details="fixture-1",
        payload=None,
        created_at=created_at,
        prev_event_hash=None,
        hash_algo=EVENT_HASH_ALGO,
    )
    tenant_a_head = PostgresStore._compute_event_hash(
        claim_id=2,
        event_type="ingest",
        from_status=None,
        to_status="candidate",
        details="fixture-2",
        payload=None,
        created_at=created_at,
        prev_event_hash=None,
        tenant_id="tenant-a",
        hash_algo=TENANT_EVENT_HASH_ALGO,
    )
    tenant_b_head = PostgresStore._compute_event_hash(
        claim_id=3,
        event_type="ingest",
        from_status=None,
        to_status="candidate",
        details="fixture-3",
        payload=None,
        created_at=created_at,
        prev_event_hash=None,
        tenant_id="tenant-b",
        hash_algo=TENANT_EVENT_HASH_ALGO,
    )
    rows = [
        event_row(1, tenant_id=None, hash_algo=EVENT_HASH_ALGO, previous=None, event_hash=v1_head),
        event_row(2, tenant_id="tenant-a", hash_algo=TENANT_EVENT_HASH_ALGO, previous=None, event_hash=tenant_a_head),
        event_row(3, tenant_id="tenant-b", hash_algo=TENANT_EVENT_HASH_ALGO, previous=None, event_hash=tenant_b_head),
        event_row(4, tenant_id="tenant-a", hash_algo=TENANT_EVENT_HASH_ALGO, previous=None, event_hash=None),
        event_row(5, tenant_id=None, hash_algo=EVENT_HASH_ALGO, previous=None, event_hash=None),
    ]
    conn = RecordingConnection(rows=rows)

    assert PostgresStore._backfill_event_chain(conn) == 2

    updates = {
        params[3]: params
        for sql, params in conn.cursor_instance.executed
        if sql.startswith("UPDATE events SET prev_event_hash")
    }
    assert updates[4][0] == tenant_a_head
    assert updates[4][2] == TENANT_EVENT_HASH_ALGO
    assert updates[5][0] == v1_head
    assert updates[5][2] == EVENT_HASH_ALGO

    with pytest.raises(RuntimeError, match="mixed v1/tenant-v2"):
        PostgresStore._backfill_event_chain(
            RecordingConnection(rows=rows),
            rebuild_all=True,
        )
