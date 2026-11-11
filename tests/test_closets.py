"""Tests for v3.9.0 F6 — Closets layer (search-side wiki-pointer boost)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.knowledge.closets import (
    ensure_closets_schema,
    extract_closet_terms,
    rebuild_closets,
    search_closets,
)


VALID_ARTICLE = """---
title: Recall Hook
description: Description that meets the schema length window of 50 to 300 chars for the validator.
type: architecture
scope: project:memorymaster
tags: [architecture]
date: 2026-04-27
claims: [123, 456, 789]
---

# Recall Hook

The recall hook uses `MemPalace`-style closets and `BM25` rescoring. Linked from
[[wiki-engine]] and [[claim-lifecycle]]. The `ChromaDB` integration ranks via
`FastAPI` endpoints.
"""


def test_ensure_closets_schema_idempotent(tmp_path):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    try:
        ensure_closets_schema(conn)
        ensure_closets_schema(conn)  # second call must not raise
        # closets + closets_fts both exist
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual')"
            )
        }
        assert "closets" in tables
        # FTS5 virtual tables register as 'table' too in sqlite_master
        assert "closets_fts" in tables
    finally:
        conn.close()


def test_extract_closet_terms_camelcase():
    out = extract_closet_terms("We use MemPalace and ChromaDB for vectors.")
    tokens = out.split()
    assert "MemPalace" in tokens
    assert "ChromaDB" in tokens


def test_extract_closet_terms_fenced_code():
    out = extract_closet_terms("Configure `claim_type` and `recall_hook` settings.")
    tokens = out.split()
    assert "claim_type" in tokens
    assert "recall_hook" in tokens


def test_extract_closet_terms_wikilinks():
    out = extract_closet_terms("See [[wiki-engine]] and [[claim-lifecycle|the lifecycle]].")
    tokens = out.split()
    assert "wiki-engine" in tokens
    assert "claim-lifecycle" in tokens


def test_extract_closet_terms_dedup():
    out = extract_closet_terms(
        "MemPalace MemPalace `MemPalace` MemPalace appears four times."
    )
    tokens = out.split()
    # Case-insensitive dedup should reduce all 4 mentions to a single term
    assert sum(1 for t in tokens if t.lower() == "mempalace") == 1


def test_extract_closet_terms_empty_body():
    assert extract_closet_terms("") == ""


def test_rebuild_closets_walks_vault(tmp_path):
    vault = tmp_path / "vault" / "wiki"
    vault.mkdir(parents=True)
    (vault / "recall-hook.md").write_text(VALID_ARTICLE, encoding="utf-8")
    (vault / "_index.md").write_text("# Index\n\nNot indexed.\n", encoding="utf-8")
    db = tmp_path / "test.db"
    counters = rebuild_closets(db, vault)
    assert counters["articles_indexed"] == 1
    assert counters["skipped"] == 1  # _index.md was skipped


def test_rebuild_closets_returns_zero_for_missing_dir(tmp_path):
    db = tmp_path / "test.db"
    counters = rebuild_closets(db, tmp_path / "does-not-exist")
    assert counters == {"articles_indexed": 0, "skipped": 0}


def test_search_closets_returns_matching_slug(tmp_path):
    vault = tmp_path / "vault" / "wiki"
    vault.mkdir(parents=True)
    (vault / "recall-hook.md").write_text(VALID_ARTICLE, encoding="utf-8")
    db = tmp_path / "test.db"
    rebuild_closets(db, vault)
    hits = search_closets(db, "MemPalace closets", limit=5)
    assert len(hits) == 1
    slug, claim_ids = hits[0]
    assert slug == "recall-hook"
    assert claim_ids == [123, 456, 789]


def test_search_closets_returns_empty_for_empty_query(tmp_path):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    ensure_closets_schema(conn)
    conn.close()
    assert search_closets(db, "", limit=5) == []
    assert search_closets(db, "   ", limit=5) == []


def test_search_closets_returns_empty_when_no_alphanumeric_tokens(tmp_path):
    """Query is purely punctuation → no tokens → []."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    ensure_closets_schema(conn)
    conn.close()
    assert search_closets(db, "!!! ??? ...", limit=5) == []


def test_search_closets_returns_empty_when_no_matches(tmp_path):
    vault = tmp_path / "vault" / "wiki"
    vault.mkdir(parents=True)
    (vault / "recall-hook.md").write_text(VALID_ARTICLE, encoding="utf-8")
    db = tmp_path / "test.db"
    rebuild_closets(db, vault)
    hits = search_closets(db, "completely unrelated zoology amphibian", limit=5)
    assert hits == []
