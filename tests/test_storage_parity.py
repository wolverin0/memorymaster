"""SQLite/Postgres storage parity tests.

The module is intentionally skipped unless POSTGRES_TEST_URL points at a
reachable test database. Postgres rows are truncated between tests, so do not
point POSTGRES_TEST_URL at a non-test database.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Iterator

import pytest

from memorymaster.lifecycle import transition_claim
from memorymaster.models import CitationInput, Claim
from memorymaster.stores.postgres_store import PostgresStore
from memorymaster.stores.storage import SQLiteStore

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_TEST_URL, reason="POSTGRES_TEST_URL is not set")


class InMemorySQLiteStore(SQLiteStore):
    def __init__(self) -> None:
        self.db_path = ":memory:"
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def connect(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()


@pytest.fixture()
def stores() -> Iterator[tuple[SQLiteStore, PostgresStore]]:
    psycopg = pytest.importorskip("psycopg")
    try:
        with psycopg.connect(POSTGRES_TEST_URL, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - depends on local Postgres
        pytest.skip(f"Postgres test database is unreachable: {exc}")

    sqlite_store = InMemorySQLiteStore()
    postgres_store = PostgresStore(POSTGRES_TEST_URL)
    try:
        sqlite_store.init_db()
        postgres_store.init_db()
        _reset_postgres(postgres_store)
        yield sqlite_store, postgres_store
    finally:
        sqlite_store.close()
        try:
            _reset_postgres(postgres_store)
        except Exception:
            pass


def _reset_postgres(store: PostgresStore) -> None:
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            TRUNCATE TABLE
                claim_links,
                claim_embeddings,
                citations,
                events,
                claims
            RESTART IDENTITY CASCADE
            """
        )


def _citation(source: str = "parity", locator: str = "turn-1", excerpt: str = "evidence") -> CitationInput:
    return CitationInput(source=source, locator=locator, excerpt=excerpt)


def _create_claim(store, index: int, *, scope: str = "project:storage-parity") -> Claim:
    return store.create_claim(
        f"storage parity claim {index:02d} contains keyword-{index:02d}",
        [_citation(locator=f"turn-{index}", excerpt=f"excerpt-{index}")],
        idempotency_key=f"storage-parity-{index:02d}",
        claim_type="fact",
        subject=f"parity-subject-{index:02d}",
        predicate="matches",
        object_value=f"value-{index:02d}",
        scope=scope,
        confidence=0.7,
    )


def _payload(claim: Claim) -> bytes:
    citations = [
        {
            "source": citation.source,
            "locator": citation.locator,
            "excerpt": citation.excerpt,
        }
        for citation in claim.citations
    ]
    body = {
        "subject": claim.subject,
        "predicate": claim.predicate,
        "object_value": claim.object_value,
        "status": claim.status,
        "scope": claim.scope,
        "confidence": claim.confidence,
        "citations": citations,
    }
    return repr(body).encode("utf-8")


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _list_claims_page(store, *, limit: int, offset: int, scope: str) -> list[Claim]:
    """Mirror list_claims ordering with OFFSET until the public API exposes it."""
    if isinstance(store, PostgresStore):
        sql = """
            SELECT * FROM claims
            WHERE scope = %s AND status <> 'archived'
            ORDER BY pinned DESC, confidence DESC, updated_at DESC, id DESC
            LIMIT %s OFFSET %s
        """
        with store.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (scope, limit, offset))
            rows = cur.fetchall()
        return [store._row_to_claim(row) for row in rows]

    sql = """
        SELECT * FROM claims
        WHERE scope = ? AND status <> 'archived'
        ORDER BY pinned DESC, confidence DESC, updated_at DESC, id DESC
        LIMIT ? OFFSET ?
    """
    with store.connect() as conn:
        rows = conn.execute(sql, (scope, limit, offset)).fetchall()
    return [store._row_to_claim(row) for row in rows]


def test_ingest_roundtrip(stores: tuple[SQLiteStore, PostgresStore]) -> None:
    sqlite_store, postgres_store = stores
    for store in stores:
        store.create_claim(
            "roundtrip parity claim has omega-keyword",
            [_citation(excerpt="roundtrip evidence")],
            idempotency_key="roundtrip-parity",
            claim_type="fact",
            subject="roundtrip-subject",
            predicate="has_value",
            object_value="roundtrip-object",
            scope="project:storage-parity",
            confidence=0.83,
        )

    sqlite_result = sqlite_store.list_claims(
        text_query="omega-keyword",
        include_citations=True,
        scope_allowlist=["project:storage-parity"],
    )[0]
    postgres_result = postgres_store.list_claims(
        text_query="omega-keyword",
        include_citations=True,
        scope_allowlist=["project:storage-parity"],
    )[0]

    assert _payload(sqlite_result) == _payload(postgres_result)


def test_list_claims_pagination(stores: tuple[SQLiteStore, PostgresStore]) -> None:
    sqlite_store, postgres_store = stores
    scope = "project:storage-parity-pagination"
    for store in stores:
        for index in range(50):
            _create_claim(store, index, scope=scope)

    sqlite_page_1 = [claim.id for claim in _list_claims_page(sqlite_store, limit=20, offset=0, scope=scope)]
    sqlite_page_2 = [claim.id for claim in _list_claims_page(sqlite_store, limit=20, offset=20, scope=scope)]
    postgres_page_1 = [claim.id for claim in _list_claims_page(postgres_store, limit=20, offset=0, scope=scope)]
    postgres_page_2 = [claim.id for claim in _list_claims_page(postgres_store, limit=20, offset=20, scope=scope)]

    assert sqlite_page_1 == postgres_page_1
    assert sqlite_page_2 == postgres_page_2


def test_status_transition(stores: tuple[SQLiteStore, PostgresStore]) -> None:
    outcomes = []
    for store in stores:
        claim = _create_claim(store, 1, scope="project:storage-parity-status")
        before = store.get_claim(claim.id, include_citations=True)
        assert before is not None

        updated = transition_claim(
            store,
            claim.id,
            "confirmed",
            "parity validation",
            event_type="validator",
        )
        outcomes.append(
            {
                "status": updated.status,
                "last_validated_at": updated.last_validated_at is not None,
                "updated_not_older": _timestamp(updated.updated_at) >= _timestamp(before.updated_at),
            }
        )

    assert outcomes[0] == outcomes[1]
    assert outcomes[0] == {
        "status": "confirmed",
        "last_validated_at": True,
        "updated_not_older": True,
    }


def test_supersede_pair(stores: tuple[SQLiteStore, PostgresStore]) -> None:
    pairs = []
    for store in stores:
        old_claim = _create_claim(store, 1, scope="project:storage-parity-supersede")
        new_claim = _create_claim(store, 2, scope="project:storage-parity-supersede")

        store.mark_superseded(old_claim.id, new_claim.id, "new claim replaces old claim")

        old_after = store.get_claim(old_claim.id, include_citations=False)
        new_after = store.get_claim(new_claim.id, include_citations=False)
        assert old_after is not None
        assert new_after is not None
        pairs.append(
            {
                "old_status": old_after.status,
                "old_replaced_by": old_after.replaced_by_claim_id,
                "new_supersedes": new_after.supersedes_claim_id,
            }
        )

    assert pairs[0] == pairs[1]
    assert pairs[0] == {
        "old_status": "superseded",
        "old_replaced_by": 2,
        "new_supersedes": 1,
    }


def test_fts_or_equivalent_search(stores: tuple[SQLiteStore, PostgresStore]) -> None:
    # SQLite uses FTS5 when available while Postgres currently falls back to LIKE.
    # Relevance scores/order can diverge, so parity is the matching logical ID set.
    sqlite_store, postgres_store = stores
    fixtures = [
        ("alpha", "marsupial-alpha-token"),
        ("bravo", "volcanic-bravo-token"),
        ("charlie", "neutrino-charlie-token"),
        ("delta", "aurora-delta-token"),
        ("echo", "cobalt-echo-token"),
    ]
    for store in stores:
        for index, (label, token) in enumerate(fixtures, start=1):
            store.create_claim(
                f"{label} search parity text includes {token}",
                [_citation(locator=f"search-{index}")],
                idempotency_key=f"search-parity-{label}",
                claim_type="fact",
                subject=f"search-{label}",
                predicate="contains",
                object_value=token,
                scope="project:storage-parity-search",
                confidence=0.6,
            )

    sqlite_hits = sqlite_store.list_claims(
        text_query="neutrino-charlie-token",
        scope_allowlist=["project:storage-parity-search"],
    )
    postgres_hits = postgres_store.list_claims(
        text_query="neutrino-charlie-token",
        scope_allowlist=["project:storage-parity-search"],
    )

    assert {claim.idempotency_key for claim in sqlite_hits} == {claim.idempotency_key for claim in postgres_hits}
    assert {claim.idempotency_key for claim in sqlite_hits} == {"search-parity-charlie"}
