"""Tests for v3.11.0 P1 + P2 + P3 fixes."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.recall import context_hook
from memorymaster.recall.claim_edges import (
    SHARES_ENTITY_KIND,
    rebuild_edges,
)
from memorymaster.knowledge.closets import (
    ensure_closets_schema,
    rebuild_closets,
    search_closets,
)


# --- P1 — F6 BM25-scaled scoring + boost-only mode -------------------------


def test_p1_search_closets_with_scores_returns_normalised(tmp_path):
    """search_closets(with_scores=True) returns 3-tuples with score in [0, 1]."""
    vault = tmp_path / "vault" / "wiki"
    vault.mkdir(parents=True)
    (vault / "alpha.md").write_text(
        "---\ntitle: a\ndescription: a\ntype: x\nscope: y\ntags: []\ndate: 2026-04-27\nclaims: [1]\n---\n\nMemPalace closets MemPalace pattern.\n",
        encoding="utf-8",
    )
    (vault / "beta.md").write_text(
        "---\ntitle: b\ndescription: b\ntype: x\nscope: y\ntags: []\ndate: 2026-04-27\nclaims: [2]\n---\n\nMemPalace mentioned briefly.\n",
        encoding="utf-8",
    )
    db = tmp_path / "test.db"
    rebuild_closets(db, vault)
    hits = search_closets(db, "MemPalace closets", limit=5, with_scores=True)
    assert len(hits) >= 1
    for hit in hits:
        assert len(hit) == 3
        slug, claim_ids, score = hit
        assert 0.0 <= score <= 1.0
    # First hit should have score=1.0 (best match normalised to 1.0)
    assert hits[0][2] == pytest.approx(1.0)


def test_p1_legacy_search_closets_still_returns_2_tuples(tmp_path):
    """Backwards-compat: search_closets() without with_scores still returns 2-tuples."""
    vault = tmp_path / "vault" / "wiki"
    vault.mkdir(parents=True)
    (vault / "x.md").write_text(
        "---\ntitle: x\ndescription: x\ntype: t\nscope: s\ntags: []\ndate: 2026-04-27\nclaims: [1]\n---\n\nMemPalace.\n",
        encoding="utf-8",
    )
    db = tmp_path / "test.db"
    rebuild_closets(db, vault)
    hits = search_closets(db, "MemPalace")
    assert len(hits) >= 1
    for hit in hits:
        assert len(hit) == 2  # not 3


def test_p1_closets_boost_only_default_off():
    assert context_hook._closets_boost_only() is False


def test_p1_closets_boost_only_via_env(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_CLOSETS_BOOST_ONLY", "1")
    assert context_hook._closets_boost_only() is True


# --- P2 — F1 swap to query_classifier --------------------------------------


def test_p2_query_classifier_recognised_in_recall():
    """The query_classifier module's classify_query is callable."""
    from memorymaster.recall.query_classifier import classify_query

    assert classify_query("what database does this use?") == "fact_lookup"
    assert classify_query("we must use postgres") == "constraint_check"
    assert classify_query("when was that changed?") == "temporal"


# --- P3 — F8 shares_entity edges -------------------------------------------


def _make_db_with_entities(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            """
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                text TEXT,
                human_id TEXT,
                replaced_by_claim_id INTEGER,
                entity_id INTEGER
            );
            INSERT INTO claims (id, text, human_id, replaced_by_claim_id, entity_id) VALUES
              (1, 'Mentions FastAPI', 'mm-1111', NULL, 100),
              (2, 'Also mentions FastAPI', 'mm-2222', NULL, 100),
              (3, 'Third claim about FastAPI', 'mm-3333', NULL, 100),
              (4, 'About a different entity', 'mm-4444', NULL, 200),
              (5, 'No entity at all', 'mm-5555', NULL, NULL);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_p3_shares_entity_edges_written(tmp_path):
    """Three claims share entity_id=100 → 3 pairwise edges (1-2, 1-3, 2-3)."""
    db = _make_db_with_entities(tmp_path)
    counters = rebuild_edges(db, include_shares_entity=True)
    assert counters["shares_entity_edges"] == 3
    # Verify in table
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT src_claim_id, dst_claim_id FROM claim_edges WHERE edge_kind = ?",
            (SHARES_ENTITY_KIND,),
        ).fetchall()
    finally:
        conn.close()
    pairs = {(min(s, d), max(s, d)) for s, d in rows}
    assert pairs == {(1, 2), (1, 3), (2, 3)}


def test_p3_singleton_entity_produces_no_edges(tmp_path):
    """Claim 4 is the only one with entity_id=200; no shares_entity edges."""
    db = _make_db_with_entities(tmp_path)
    rebuild_edges(db, include_shares_entity=True)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT * FROM claim_edges WHERE edge_kind = ? AND (src_claim_id = 4 OR dst_claim_id = 4)",
            (SHARES_ENTITY_KIND,),
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


def test_p3_max_per_pivot_caps_explosion(tmp_path):
    """When >max claims share an entity, the entire group is skipped (overflow guard)."""
    db = tmp_path / "many.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            "CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT, human_id TEXT, replaced_by_claim_id INTEGER, entity_id INTEGER);"
        )
        for i in range(1, 11):
            conn.execute(
                "INSERT INTO claims VALUES (?, ?, ?, NULL, 999)",
                (i, f"claim {i}", f"mm-{i:04d}"),
            )
        conn.commit()
    finally:
        conn.close()
    # Cap at 5 — group of 10 won't qualify (HAVING COUNT(*) BETWEEN 2 AND 5)
    counters = rebuild_edges(db, shares_entity_max_per_pivot=5)
    assert counters["shares_entity_edges"] == 0


def test_p3_shares_entity_disabled_when_flag_off(tmp_path):
    db = _make_db_with_entities(tmp_path)
    counters = rebuild_edges(db, include_shares_entity=False)
    assert counters["shares_entity_edges"] == 0
