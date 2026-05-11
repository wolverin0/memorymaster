from __future__ import annotations

import os
from pathlib import Path

import pytest

from memorymaster.models import CitationInput
from memorymaster.postgres_store import PostgresStore
from memorymaster.storage import SQLiteStore


@pytest.fixture(params=["sqlite", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "sqlite":
        sqlite_store = SQLiteStore(tmp_path / "storage-parity.db")
        sqlite_store.init_db()
        return sqlite_store

    dsn = os.getenv("POSTGRES_URL") or os.getenv("MEMORYMASTER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Postgres DSN not set")
    pg_store = PostgresStore(dsn)
    pg_store.init_db()
    return pg_store


def _claim(store, text: str):
    return store.create_claim(
        text,
        [CitationInput(source="storage-parity", locator=text, excerpt=text)],
    )


def test_sqlite_postgres_list_citations_batch_matches_single_lookup(store) -> None:
    first = _claim(store, "batch citation alpha")
    second = _claim(store, "batch citation beta")

    batch = store.list_citations_batch([first.id, second.id])

    assert [citation.source for citation in batch[first.id]] == ["storage-parity"]
    assert [citation.source for citation in batch[second.id]] == ["storage-parity"]


def test_sqlite_postgres_count_citations_batch_matches_single_lookup(store) -> None:
    first = _claim(store, "batch count alpha")
    second = _claim(store, "batch count beta")

    assert store.count_citations_batch([first.id, second.id]) == {
        first.id: store.count_citations(first.id),
        second.id: store.count_citations(second.id),
    }


def test_sqlite_postgres_set_normalized_texts_batch_updates_claims(store) -> None:
    first = _claim(store, "normalize alpha")
    second = _claim(store, "normalize beta")

    store.set_normalized_texts_batch(
        {
            first.id: "normalized alpha",
            second.id: "normalized beta",
        }
    )

    assert store.get_claim(first.id, include_citations=False).normalized_text == "normalized alpha"
    assert store.get_claim(second.id, include_citations=False).normalized_text == "normalized beta"
