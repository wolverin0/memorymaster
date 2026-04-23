"""Tests for ``memorymaster.verbatim_recall``.

Covers:
  1. FTS5 MATCH semantics — multi-token AND, quoting, special characters.
  2. Scope filter — prefix LIKE matches sub-scopes, unrelated scopes excluded.
  3. Opt-in gate — ``is_enabled()`` honours MEMORYMASTER_RECALL_VERBATIM.
  4. Weight knob — ``verbatim_weight()`` reads env, falls back on garbage.
  5. Degradation — empty table and missing tables return [].
  6. Synthetic row shape — ranker reads the expected attributes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.verbatim_recall import (
    VerbatimHit,
    _build_match_expr,
    _escape_fts5_token,
    hit_to_synthetic_row,
    is_enabled,
    recall_verbatim,
    verbatim_weight,
)


# --------------------------------------------------------------------------- #
# Fixture helpers — a minimal in-file DB with verbatim_memories + verbatim_fts.
# --------------------------------------------------------------------------- #

def _init_verbatim_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE verbatim_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'project',
            timestamp TEXT NOT NULL,
            source_agent TEXT DEFAULT '',
            embedding_synced INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE VIRTUAL TABLE verbatim_fts USING fts5(
            content,
            content='verbatim_memories',
            content_rowid='id',
            tokenize='porter unicode61'
        );
        """
    )


def _insert_verbatim(
    conn: sqlite3.Connection,
    content: str,
    *,
    scope: str = "project:test",
    session_id: str = "s1",
    role: str = "user",
) -> int:
    cur = conn.execute(
        """INSERT INTO verbatim_memories (session_id, role, content, scope, timestamp)
           VALUES (?, ?, ?, ?, '2026-04-22T00:00:00Z')""",
        (session_id, role, content, scope),
    )
    row_id = cur.lastrowid
    conn.execute(
        "INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)",
        (row_id, content),
    )
    conn.commit()
    return int(row_id)


@pytest.fixture
def verbatim_db(tmp_path: Path) -> str:
    """Build a fresh verbatim-schema DB with a handful of rows."""
    db = tmp_path / "verbatim.db"
    conn = sqlite3.connect(db)
    _init_verbatim_schema(conn)
    _insert_verbatim(
        conn,
        "steward tuning finished cleanly and recall shipped on time",
        scope="project:test",
    )
    _insert_verbatim(
        conn,
        "qdrant vector search deployment is stable",
        scope="project:test",
    )
    _insert_verbatim(
        conn,
        "wiki absorb flow refactored yesterday",
        scope="project:other",
    )
    _insert_verbatim(
        conn,
        "unrelated banana bread recipe version two",
        scope="project:test",
    )
    conn.close()
    return str(db)


# --------------------------------------------------------------------------- #
# 1. FTS5 MATCH semantics
# --------------------------------------------------------------------------- #

def test_match_expr_strips_stopwords_and_and_joins() -> None:
    expr = _build_match_expr("the steward is tuning")
    # "the" and "is" are stopwords; "steward" and "tuning" should remain,
    # quoted and joined with AND.
    assert expr == '"steward" AND "tuning"'


def test_match_expr_empty_when_all_stopwords() -> None:
    assert _build_match_expr("the and or but") == ""


def test_match_expr_dedups_tokens() -> None:
    expr = _build_match_expr("steward steward tuning steward")
    # Single "steward" AND "tuning".
    assert expr.count("steward") == 1
    assert "tuning" in expr


def test_match_expr_caps_at_six_tokens() -> None:
    expr = _build_match_expr(
        "alpha bravo charlie delta echo foxtrot golf hotel india"
    )
    # Six tokens -> five ANDs; more tokens get truncated.
    assert expr.count(" AND ") == 5


def test_escape_fts5_token_handles_quotes() -> None:
    assert _escape_fts5_token('fix"me') == '"fix""me"'


def test_recall_verbatim_returns_hit_for_matching_token(verbatim_db: str) -> None:
    # Single-token query so _build_match_expr doesn't AND-join unrelated
    # tokens out of the match.
    hits = recall_verbatim(
        "steward",
        scope=None,
        db_path=verbatim_db,
        limit=5,
    )
    assert len(hits) >= 1
    assert any("steward" in h.excerpt for h in hits)


def test_recall_verbatim_and_semantics_filters_unrelated(verbatim_db: str) -> None:
    # "banana" is only in one row; "steward" only in another. AND of both
    # would match zero rows. Test a query with both words to confirm FTS
    # is doing AND (not OR).
    hits = recall_verbatim(
        "banana steward",
        scope=None,
        db_path=verbatim_db,
        limit=5,
    )
    assert hits == []


def test_recall_verbatim_multi_token_and_hit(verbatim_db: str) -> None:
    # Both tokens are in the same row — should hit.
    hits = recall_verbatim(
        "steward tuning",
        scope=None,
        db_path=verbatim_db,
        limit=5,
    )
    assert len(hits) >= 1
    assert any("steward" in h.excerpt and "tuning" in h.excerpt for h in hits)


# --------------------------------------------------------------------------- #
# 2. Scope filter
# --------------------------------------------------------------------------- #

def test_scope_filter_limits_results(verbatim_db: str) -> None:
    # Use a single token known to appear in multiple rows across scopes.
    hits_all = recall_verbatim(
        "steward",
        scope=None,
        db_path=verbatim_db,
        limit=10,
    )
    hits_test = recall_verbatim(
        "steward",
        scope="project:test",
        db_path=verbatim_db,
        limit=10,
    )
    # steward only appears in project:test — so filter should be a no-op.
    assert {h.scope for h in hits_test} <= {"project:test"}
    # The absorb row is in project:other; prove scope LIKE filters it out.
    absorb_all = recall_verbatim("absorb", None, verbatim_db, 10)
    absorb_test = recall_verbatim("absorb", "project:test", verbatim_db, 10)
    assert len(absorb_all) >= 1
    assert absorb_test == []


def test_scope_prefix_match(verbatim_db: str) -> None:
    # Scope "project" prefix should match both "project:test" and "project:other".
    hits = recall_verbatim(
        "absorb",
        scope="project",
        db_path=verbatim_db,
        limit=10,
    )
    scopes = {h.scope for h in hits}
    assert scopes <= {"project:test", "project:other"}
    assert "project:other" in scopes  # absorb row lives here


# --------------------------------------------------------------------------- #
# 3. Opt-in gate
# --------------------------------------------------------------------------- #

def test_is_enabled_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORYMASTER_RECALL_VERBATIM", raising=False)
    assert is_enabled() is False


def test_is_enabled_respects_env_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_RECALL_VERBATIM", "1")
    assert is_enabled() is True
    for val in ("0", "false", "False", "no", "off", ""):
        monkeypatch.setenv("MEMORYMASTER_RECALL_VERBATIM", val)
        assert is_enabled() is False


# --------------------------------------------------------------------------- #
# 4. Weight knob
# --------------------------------------------------------------------------- #

def test_verbatim_weight_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORYMASTER_RECALL_W_VERBATIM", raising=False)
    assert verbatim_weight() == 0.0


def test_verbatim_weight_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_VERBATIM", "0.5")
    assert verbatim_weight() == 0.5


def test_verbatim_weight_garbage_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_VERBATIM", "not-a-number")
    assert verbatim_weight() == 0.0


# --------------------------------------------------------------------------- #
# 5. Degradation paths
# --------------------------------------------------------------------------- #

def test_empty_table_returns_empty(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    _init_verbatim_schema(conn)
    conn.close()
    assert recall_verbatim("steward tuning", None, str(db), limit=5) == []


def test_missing_tables_returns_empty(tmp_path: Path) -> None:
    db = tmp_path / "no-verbatim.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()
    # No verbatim_* tables at all — must degrade silently.
    assert recall_verbatim("steward tuning", None, str(db), limit=5) == []


def test_unparseable_query_returns_empty(verbatim_db: str) -> None:
    # Pure stopwords -> no tokens survive -> empty result.
    assert recall_verbatim("the and or", None, verbatim_db, limit=5) == []


def test_nonexistent_db_returns_empty(tmp_path: Path) -> None:
    ghost = tmp_path / "does-not-exist.db"
    assert recall_verbatim("steward tuning", None, str(ghost), limit=5) == []


def test_score_is_positive_oriented(verbatim_db: str) -> None:
    hits = recall_verbatim("steward tuning", None, verbatim_db, limit=5)
    assert hits
    # FTS5 returns negative ranks; verbatim_recall flips sign so
    # higher == better for the downstream ranker.
    assert all(h.score >= 0 for h in hits)


# --------------------------------------------------------------------------- #
# 6. Synthetic row shape for the ranker
# --------------------------------------------------------------------------- #

def test_synthetic_row_has_ranker_attributes() -> None:
    hit = VerbatimHit(
        verbatim_id=42,
        scope="project:test",
        excerpt="steward tuning finished",
        score=1.23,
        session_id="s1",
        role="user",
    )
    row = hit_to_synthetic_row(hit)
    assert row["source"] == "verbatim"
    assert row["verbatim_score"] == pytest.approx(1.23)
    # Claim-like object with text, scope and a negative id (reserved space).
    claim = row["claim"]
    assert claim.text == "steward tuning finished"
    assert claim.scope == "project:test"
    assert claim.id < 0
    # Every other score is 0 so only W_VERBATIM contributes.
    for field in ("lexical_score", "freshness_score", "confidence_score",
                  "vector_score", "entity_score"):
        assert row[field] == 0.0
