from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.recall.recall_tokenizer import _corpus_stats, extract_query_tokens
from memorymaster.core.models import CitationInput
from memorymaster.stores.storage import SQLiteStore
from memorymaster.surfaces.mcp_server import _bounded_limit


def test_event_queries_use_versioned_composite_indexes(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    store = SQLiteStore(db)
    store.init_db()
    with sqlite3.connect(db) as conn:
        indexes = {
            str(row[1])
            for row in conn.execute("PRAGMA index_list(events)").fetchall()
        }
        assert "idx_events_type_created_id" in indexes
        assert "idx_events_type_details_created" in indexes
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM events "
            "WHERE event_type=? ORDER BY created_at DESC, id DESC LIMIT 10",
            ("system",),
        ).fetchall()
    assert "idx_events_type_created_id" in " ".join(str(row) for row in plan)


def test_token_stats_invalidate_on_corpus_generation(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = SQLiteStore(db)
    store.init_db()
    store.create_claim(
        "alpha steward", [CitationInput(source="test")], scope="project:test"
    )
    _corpus_stats.cache_clear()

    assert "alpha" in extract_query_tokens("alpha steward", str(db), max_tokens=2)
    before = _corpus_stats.cache_info()
    store.create_claim(
        "qdrant qdrant", [CitationInput(source="test")], scope="project:test"
    )
    extract_query_tokens("qdrant steward", str(db), max_tokens=2)
    after = _corpus_stats.cache_info()

    assert after.misses == before.misses + 1


@pytest.mark.parametrize(
    ("value", "maximum", "expected"),
    [(-5, 100, 1), (0, 100, 1), (20, 100, 20), (10_000, 100, 100)],
)
def test_mcp_limits_are_clamped(value: int, maximum: int, expected: int) -> None:
    assert _bounded_limit(value, maximum=maximum) == expected


def test_store_keyset_pages_do_not_repeat_rows(tmp_path: Path) -> None:
    db = tmp_path / "pages.db"
    store = SQLiteStore(db)
    store.init_db()
    for index in range(5):
        store.create_claim(
            f"claim {index}",
            [CitationInput(source="test")],
            scope="project:test",
            confidence=0.5,
        )

    first, cursor = store.list_claims_page(limit=2)
    second, next_cursor = store.list_claims_page(limit=2, cursor=cursor)

    assert len(first) == len(second) == 2
    assert {claim.id for claim in first}.isdisjoint(claim.id for claim in second)
    assert cursor and next_cursor and cursor != next_cursor


def test_retrieval_rows_reference_claims_without_duplicate_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memorymaster.core.lifecycle import transition_claim
    from memorymaster.core.service import MemoryService
    from memorymaster.surfaces.mcp_server import _project_scope, query_memory

    monkeypatch.delenv("QDRANT_URL", raising=False)
    service = MemoryService(tmp_path / "mcp.db", workspace_root=tmp_path)
    service.init_db()
    claim = service.ingest(
        text="bounded retrieval payload",
        citations=[CitationInput(source="test")],
        scope=_project_scope(str(tmp_path)),
        source_agent="test",
    )
    transition_claim(
        service.store,
        claim.id,
        "confirmed",
        reason="test fixture",
        event_type="validator",
    )
    result = query_memory(
        query="bounded retrieval payload",
        db=str(service.store.db_path),
        workspace=str(tmp_path),
        trust_mode="exploratory",
    )

    assert result["response_contract"] == "memorymaster.retrieval.v2"
    assert result["rows_data"]
    assert all("claim" not in row for row in result["rows_data"])
    assert all(0 <= row["claim_index"] < len(result["claims"]) for row in result["rows_data"])
