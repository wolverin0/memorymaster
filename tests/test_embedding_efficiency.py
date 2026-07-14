from __future__ import annotations

import json
from dataclasses import replace

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall.embeddings import hash_embed
from memorymaster.stores.storage import SQLiteStore


class _CountingProvider:
    model = "counting-v1"
    is_semantic = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append((text,))
        return hash_embed(text, dims=16)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(tuple(texts))
        return [hash_embed(text, dims=16) for text in texts]


def _claim(store: SQLiteStore, text: str):
    return store.create_claim(
        text=text,
        citations=[CitationInput(source="test")],
    )


def test_warm_vector_scores_embed_query_once_and_do_not_rewrite_candidates(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "warm.db")
    store.init_db()
    claims = [_claim(store, "alpha memory"), _claim(store, "beta memory")]
    provider = _CountingProvider()

    store.vector_scores("memory", claims, provider)
    with store.connect() as conn:
        before = [
            tuple(row)
            for row in conn.execute(
                "SELECT claim_id, model, content_hash, embedding_json, updated_at "
                "FROM claim_embeddings ORDER BY claim_id"
            ).fetchall()
        ]

    provider.calls.clear()
    store.vector_scores("memory", claims, provider)
    with store.connect() as conn:
        after = [
            tuple(row)
            for row in conn.execute(
                "SELECT claim_id, model, content_hash, embedding_json, updated_at "
                "FROM claim_embeddings ORDER BY claim_id"
            ).fetchall()
        ]

    assert provider.calls == [("memory",)]
    assert after == before


def test_only_changed_embedding_input_is_recomputed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "stale.db")
    store.init_db()
    first = _claim(store, "first memory")
    second = _claim(store, "second memory")
    provider = _CountingProvider()
    store.upsert_embeddings([first, second], provider)
    provider.calls.clear()

    changed = replace(first, text="first memory changed")
    assert store.upsert_embeddings([changed, second], provider) == 1
    assert provider.calls == [(store._embedding_text(changed),)]

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT claim_id, content_hash, embedding_json FROM claim_embeddings ORDER BY claim_id"
        ).fetchall()
    assert len(rows) == 2
    assert all(row["content_hash"] for row in rows)
    assert all(len(json.loads(row["embedding_json"])) == 16 for row in rows)


def test_embedding_efficiency_migration_adds_hash_and_cursor_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "migration.db")
    store.init_db()
    with store.connect() as conn:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(claim_embeddings)")
        }
        cursor_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(qdrant_sync_state)")
        }
        version = int(conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0])

    assert "content_hash" in columns
    assert cursor_columns == {"stream_key", "tenant_id", "last_claim_id", "updated_at"}
    assert version >= 14


def test_service_reuses_semantic_probe_as_the_query_embedding(tmp_path) -> None:
    service = MemoryService(tmp_path / "service.db", workspace_root=tmp_path)
    service.init_db()
    service.ingest(
        text="authoritative candidate text",
        citations=[CitationInput(source="test")],
        scope="project:test",
    )
    provider = _CountingProvider()
    provider.model = "semantic-counting-v1"
    provider.is_semantic = True
    service.embedding_provider = provider

    service.query_rows(
        query_text="unique lookup query",
        retrieval_mode="hybrid",
        include_candidates=True,
        scope_allowlist=["project:test"],
    )

    query_calls = [call for call in provider.calls if call == ("unique lookup query",)]
    assert len(query_calls) == 1
