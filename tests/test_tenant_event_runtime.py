"""Runtime and reconciliation tests for the tenant-aware event ledger."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.stores._storage_shared import EVENT_HASH_ALGO
from memorymaster.stores.postgres_store import PostgresStore


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


def _postgres_store(
    claim_tenant: str = "tenant-a",
) -> tuple[PostgresStore, RecordingConnection]:
    store = PostgresStore(
        "postgresql://db.invalid/app",
        tenant_id="tenant-a",
        require_tenant=True,
    )
    store._psycopg = (object(), object(), lambda value: value)
    return store, RecordingConnection(claim_tenant)

def test_full_sqlite_reinit_preserves_existing_v1_hash_triples(tmp_path) -> None:
    service = MemoryService(
        tmp_path / "legacy-events.db",
        workspace_root=tmp_path,
        tenant_id="tenant-a",
    )
    service.init_db()
    service.ingest(
        text="Legacy event chain fixture.",
        citations=[CitationInput(source="test")],
        source_agent="tenant-event-test",
    )
    with service.store.connect() as conn:
        before = conn.execute(
            """
            SELECT id, prev_event_hash, event_hash, hash_algo
            FROM events ORDER BY id
            """
        ).fetchall()
        conn.execute("DROP TRIGGER IF EXISTS trg_events_append_only_update")
        conn.execute("UPDATE events SET tenant_event_hash = NULL, tenant_hash_algo = NULL")
        conn.commit()

    service.init_db()

    with service.store.connect() as conn:
        after = conn.execute(
            """
            SELECT id, prev_event_hash, event_hash, hash_algo
            FROM events ORDER BY id
            """
        ).fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_sqlite_new_events_persist_the_claim_tenant(tmp_path) -> None:
    service = MemoryService(
        tmp_path / "events.db",
        workspace_root=tmp_path,
        tenant_id="tenant-a",
    )
    service.init_db()
    claim = service.ingest(
        text="Tenant event fixture.",
        citations=[CitationInput(source="test")],
        source_agent="tenant-event-test",
    )

    with service.store.connect() as conn:
        tenant_ids = {
            row["tenant_id"]
            for row in conn.execute(
                "SELECT tenant_id FROM events WHERE claim_id = ?",
                (claim.id,),
            ).fetchall()
        }

    assert tenant_ids == {"tenant-a"}


def test_v1_hash_is_stable_and_v2_hash_is_tenant_bound() -> None:
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
    v1_a = PostgresStore._compute_event_hash(
        **kwargs,
        tenant_id="tenant-a",
        hash_algo=EVENT_HASH_ALGO,
    )
    v1_b = PostgresStore._compute_event_hash(
        **kwargs,
        tenant_id="tenant-b",
        hash_algo=EVENT_HASH_ALGO,
    )
    v2_a = PostgresStore._compute_event_hash(
        **kwargs,
        tenant_id="tenant-a",
        hash_algo=TENANT_EVENT_HASH_ALGO,
    )
    v2_b = PostgresStore._compute_event_hash(
        **kwargs,
        tenant_id="tenant-b",
        hash_algo=TENANT_EVENT_HASH_ALGO,
    )

    assert v1_a == v1_b
    assert v2_a != v2_b


def test_postgres_event_insert_uses_tenant_chain_and_advisory_lock() -> None:
    store, conn = _postgres_store()

    event_id = store._insert_event_row(
        conn,
        claim_id=7,
        event_type="ingest",
        from_status=None,
        to_status="candidate",
        details="fixture",
        payload={"count": 1},
        created_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    emitted = "\n".join(sql for sql, _ in conn.cursor_instance.executed)
    insert_params = next(
        params
        for sql, params in conn.cursor_instance.executed
        if "INSERT INTO events" in sql
    )
    assert event_id == 42
    assert "pg_advisory_xact_lock" in emitted
    assert "hash_algo = %s" in emitted
    assert "tenant_id IS NOT DISTINCT FROM %s" in emitted
    assert "tenant_id" in next(
        sql for sql, _ in conn.cursor_instance.executed if "INSERT INTO events" in sql
    )
    assert "tenant-a" in insert_params
    assert TENANT_EVENT_HASH_ALGO in insert_params


def test_postgres_event_insert_continues_historical_tenant_head() -> None:
    store, conn = _postgres_store()
    conn.cursor_instance = HistoricalHeadCursor()

    store._insert_event_row(
        conn,
        claim_id=7,
        event_type="ingest",
        from_status=None,
        to_status="candidate",
        details="fixture",
        payload=None,
        created_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    insert_params = next(
        params
        for sql, params in conn.cursor_instance.executed
        if "INSERT INTO events" in sql
    )
    assert insert_params[7] == "primary-v2-head"
    assert insert_params[11] == "tenant-history-head"


def test_postgres_event_write_rejects_wrong_claim_tenant() -> None:
    store, wrong_conn = _postgres_store(claim_tenant="tenant-b")

    with pytest.raises(PermissionError, match="tenant"):
        store._insert_event_row(
            wrong_conn,
            claim_id=7,
            event_type="ingest",
            from_status=None,
            to_status="candidate",
            details="fixture",
            payload=None,
            created_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )



def test_postgres_claimless_team_audit_event_uses_bound_tenant() -> None:
    store, conn = _postgres_store()

    event_id = store._insert_event_row(
        conn,
        claim_id=None,
        event_type="policy_decision",
        from_status=None,
        to_status=None,
        details="authorization_denied",
        payload={"action": "denied"},
        created_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    insert_params = next(
        params for sql, params in conn.cursor_instance.executed if "INSERT INTO events" in sql
    )
    assert event_id == 42
    assert "tenant-a" in insert_params


def test_team_list_events_has_explicit_tenant_predicate(monkeypatch) -> None:
    store, conn = _postgres_store()
    monkeypatch.setattr(store, "connect", lambda: conn)

    assert store.list_events(limit=5) == []

    sql, params = conn.cursor_instance.executed[-1]
    assert "tenant_id IS NOT DISTINCT FROM %s" in sql
    assert params[0] == "tenant-a"


def test_integrity_validation_partitions_v2_chains_by_tenant() -> None:
    rows = [
        {"id": 1, "prev_event_hash": None, "event_hash": "v1-a", "hash_algo": EVENT_HASH_ALGO, "tenant_id": "tenant-a"},
        {"id": 2, "prev_event_hash": "v1-a", "event_hash": "v1-b", "hash_algo": EVENT_HASH_ALGO, "tenant_id": "tenant-b"},
        {"id": 3, "prev_event_hash": None, "event_hash": "a-1", "hash_algo": TENANT_EVENT_HASH_ALGO, "tenant_id": "tenant-a"},
        {"id": 4, "prev_event_hash": None, "event_hash": "b-1", "hash_algo": TENANT_EVENT_HASH_ALGO, "tenant_id": "tenant-b"},
        {"id": 5, "prev_event_hash": "a-1", "event_hash": "a-2", "hash_algo": TENANT_EVENT_HASH_ALGO, "tenant_id": "tenant-a"},
        {"id": 6, "prev_event_hash": "b-1", "event_hash": "b-2", "hash_algo": TENANT_EVENT_HASH_ALGO, "tenant_id": "tenant-b"},
    ]

    assert PostgresStore._event_chain_issues(rows, limit=20, verify_content=False) == []
    rows[-1]["prev_event_hash"] = "wrong-tenant-head"
    issues = PostgresStore._event_chain_issues(rows, limit=20, verify_content=False)
    assert issues == [
        {
            "event_id": 6,
            "reason": "broken_prev_link",
            "expected_prev_event_hash": "b-1",
            "actual_prev_event_hash": "wrong-tenant-head",
        }
    ]


def test_integrity_validation_recomputes_v2_event_content_hash() -> None:
    created_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
    event_hash = PostgresStore._compute_event_hash(
        claim_id=7,
        event_type="ingest",
        from_status=None,
        to_status="candidate",
        details="fixture",
        payload={"count": 1},
        created_at=created_at,
        prev_event_hash=None,
        tenant_id="tenant-a",
        hash_algo=TENANT_EVENT_HASH_ALGO,
    )
    row = {
        "id": 1,
        "claim_id": 7,
        "event_type": "ingest",
        "from_status": None,
        "to_status": "candidate",
        "details": "fixture",
        "payload_json": {"count": 1},
        "created_at": created_at,
        "prev_event_hash": None,
        "event_hash": event_hash,
        "hash_algo": TENANT_EVENT_HASH_ALGO,
        "tenant_id": "tenant-a",
    }

    assert PostgresStore._event_chain_issues([row], limit=20) == []
    row["payload_json"] = {"count": 2}
    issues = PostgresStore._event_chain_issues([row], limit=20)

    assert any(issue["reason"] == "event_hash_mismatch" for issue in issues)


def test_integrity_validation_recomputes_v1_event_content_hash() -> None:
    created_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
    event_hash = PostgresStore._compute_event_hash(
        claim_id=7,
        event_type="ingest",
        from_status=None,
        to_status="candidate",
        details="fixture",
        payload={"count": 1},
        created_at=created_at,
        prev_event_hash=None,
        hash_algo=EVENT_HASH_ALGO,
    )
    row = {
        "id": 1,
        "claim_id": 7,
        "event_type": "ingest",
        "from_status": None,
        "to_status": "candidate",
        "details": "fixture",
        "payload_json": {"count": 1},
        "created_at": created_at,
        "prev_event_hash": None,
        "event_hash": event_hash,
        "hash_algo": EVENT_HASH_ALGO,
        "tenant_id": "tenant-a",
    }

    assert PostgresStore._event_chain_issues([row], limit=20) == []
    row["details"] = "tampered"
    issues = PostgresStore._event_chain_issues([row], limit=20)

    assert any(issue["reason"] == "event_hash_mismatch" for issue in issues)


def test_team_store_allows_only_read_only_tenant_reconciliation() -> None:
    store, conn = _postgres_store()
    store.connect = lambda: conn

    report = store.reconcile_integrity(fix=False)

    assert report["summary"]["hash_chain_issues"] == 0
    assert report["summary"]["tenant_hash_chain_issues"] == 0

    with pytest.raises(PermissionError, match="privileged maintenance"):
        store.reconcile_integrity(fix=True)


def test_postgres_read_only_reconciliation_does_not_run_schema_repairs(monkeypatch) -> None:
    store = PostgresStore("postgresql://db.invalid/app")
    conn = RecordingConnection()
    store._psycopg = (object(), object(), lambda value: value)
    monkeypatch.setattr(store, "connect", lambda: conn)

    def fail_if_called(_conn) -> None:
        pytest.fail("read-only reconciliation invoked a schema repair helper")

    monkeypatch.setattr(store, "_ensure_event_integrity_schema", fail_if_called)
    report = store.reconcile_integrity(fix=False)

    assert report["summary"]["hash_chain_issues"] == 0


def test_tenant_hash_validation_recomputes_content_commitment() -> None:
    first_hash = PostgresStore._compute_tenant_event_hash(
        tenant_id="tenant-a",
        event_hash="global-1",
        tenant_prev_event_hash=None,
    )
    second_hash = PostgresStore._compute_tenant_event_hash(
        tenant_id="tenant-a",
        event_hash="global-2",
        tenant_prev_event_hash=first_hash,
    )
    rows = [
        {
            "id": 1,
            "tenant_id": "tenant-a",
            "event_hash": "global-1",
            "tenant_prev_event_hash": None,
            "tenant_event_hash": first_hash,
            "tenant_hash_algo": TENANT_EVENT_HASH_ALGO,
        },
        {
            "id": 2,
            "tenant_id": "tenant-a",
            "event_hash": "global-2",
            "tenant_prev_event_hash": first_hash,
            "tenant_event_hash": second_hash,
            "tenant_hash_algo": TENANT_EVENT_HASH_ALGO,
        },
    ]

    assert PostgresStore._tenant_event_chain_issues(rows, limit=20) == []
    rows[1]["event_hash"] = "tampered-global-commitment"
    issues = PostgresStore._tenant_event_chain_issues(rows, limit=20)
    assert any(issue["reason"] == "tenant_hash_mismatch" for issue in issues)
