"""Unit tests for memorymaster.recall_tokenizer."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.recall_tokenizer import (
    _alias_set,
    _candidate_tokens,
    _corpus_stats,
    extract_query_tokens,
)


@pytest.fixture()
def tiny_db(tmp_path: Path) -> str:
    db_path = tmp_path / "tiny.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT);"
        "CREATE TABLE entity_aliases (id INTEGER PRIMARY KEY, entity_id INTEGER, "
        "alias TEXT UNIQUE, original_form TEXT);"
    )
    # "common" appears in every claim (low IDF). "steward"/"qdrant" are rare.
    rows = [
        "common task about the steward decay job",
        "common fact about qdrant vector backend",
        "common reminder to rotate keys",
        "common notes on confidence scoring",
        "common note about tier recomputation",
    ]
    conn.executemany("INSERT INTO claims (text) VALUES (?)", [(r,) for r in rows])
    conn.executemany(
        "INSERT INTO entity_aliases (entity_id, alias, original_form) VALUES (?, ?, ?)",
        [(1, "steward", "Steward"), (2, "qdrant", "Qdrant")],
    )
    conn.commit()
    conn.close()
    _corpus_stats.cache_clear()
    _alias_set.cache_clear()
    return str(db_path)


def test_stopword_and_length_and_digit_filters() -> None:
    toks = _candidate_tokens("hay que correr el steward y 429 ab tambien el dashboard")
    assert "steward" in toks and "dashboard" in toks
    for bad in ("que", "el", "y", "tambien", "429", "ab"):
        assert bad not in toks


def test_url_stripping() -> None:
    toks = _candidate_tokens("lee https://dev.to/marcos/path steward")
    assert "https" not in toks
    assert "steward" in toks


def test_idf_prefers_rare_over_common(tiny_db: str) -> None:
    # "common" = freq 5, "steward" = freq 1 + alias boost.
    assert extract_query_tokens("common task about the steward", tiny_db, max_tokens=1) == "steward"


def test_entity_alias_boost_breaks_tie(tiny_db: str) -> None:
    # both 'qdrant' and 'vector' appear once; aliased 'qdrant' wins.
    assert extract_query_tokens("something about qdrant vector backend", tiny_db, max_tokens=1) == "qdrant"


def test_short_prompt_passes_all_tokens(tiny_db: str) -> None:
    out = extract_query_tokens("steward qdrant", tiny_db, max_tokens=6)
    assert out.split() == ["steward", "qdrant"]


def test_empty_or_stopword_only_returns_empty(tiny_db: str) -> None:
    assert extract_query_tokens("", "/nonexistent.db") == ""
    assert extract_query_tokens("   ", "/nonexistent.db") == ""
    assert extract_query_tokens("que es lo que hay", tiny_db, max_tokens=6) == ""


def test_missing_db_does_not_crash() -> None:
    _corpus_stats.cache_clear()
    _alias_set.cache_clear()
    out = extract_query_tokens("steward dashboard qdrant", "/no/such.db", max_tokens=3)
    assert "steward" in out.split()


def test_max_tokens_edges(tiny_db: str) -> None:
    assert extract_query_tokens("steward qdrant", tiny_db, max_tokens=0) == ""
    out = extract_query_tokens(
        "migrate steward qdrant embeddings retrieval rate limiting rotation load",
        tiny_db, max_tokens=3,
    )
    assert len(out.split()) <= 3
