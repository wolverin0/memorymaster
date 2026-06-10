"""Unit tests for v3.13 pre-steward candidate dedupe."""

from __future__ import annotations

import sqlite3

import pytest

from memorymaster.govern.candidate_dedupe import (
    DedupeResult,
    jaccard_high_threshold,
    fts_candidates_in_scope,
    find_near_duplicate,
    is_enabled,
    is_shadow_mode,
)

_SCHEMA = """
CREATE TABLE claims (
    id INTEGER PRIMARY KEY,
    text TEXT,
    scope TEXT,
    status TEXT,
    replaced_by_claim_id INTEGER,
    access_count INTEGER DEFAULT 0,
    updated_at TEXT
);
CREATE VIRTUAL TABLE claims_fts USING fts5(text, content='claims', content_rowid='id');
CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
    INSERT INTO claims_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER claims_au AFTER UPDATE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO claims_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(_SCHEMA)
    c.commit()
    return c


def _ins(
    conn: sqlite3.Connection,
    *,
    text: str,
    scope: str = "project:memorymaster",
    status: str = "confirmed",
) -> int:
    cur = conn.execute(
        "INSERT INTO claims (text, scope, status) VALUES (?, ?, ?)",
        (text, scope, status),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_env_flag_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORYMASTER_DEDUPE_ENABLED", raising=False)
    monkeypatch.delenv("MEMORYMASTER_DEDUPE_SHADOW", raising=False)
    monkeypatch.delenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", raising=False)
    assert is_enabled() is False
    assert is_shadow_mode() is True
    assert jaccard_high_threshold() == pytest.approx(0.85)


def test_env_flag_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_ENABLED", "true")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_SHADOW", "0")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.5")
    assert is_enabled() is True
    assert is_shadow_mode() is False
    assert jaccard_high_threshold() == pytest.approx(0.5)


def test_env_flag_invalid_threshold_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "not-a-number")
    assert jaccard_high_threshold() == pytest.approx(0.85)


def test_too_short_text_passthrough(conn: sqlite3.Connection) -> None:
    cid = _ins(conn, text="hi")
    result = find_near_duplicate(
        conn,
        candidate_id=cid,
        candidate_text="hi",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"
    assert result.reason == "text-too-short"


def test_no_existing_claims_passthrough(conn: sqlite3.Connection) -> None:
    cid = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=cid,
        candidate_text="user prefers vim editor with vim plugins for python",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"
    assert result.reason == "no-fts-matches"


def test_paraphrase_detected_as_archive(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.5")
    canonical = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        status="confirmed",
    )
    candidate = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development tasks",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=candidate,
        candidate_text="user prefers vim editor with vim plugins for python development tasks",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "archive"
    assert result.canonical_claim_id == canonical
    assert result.jaccard_score is not None
    assert result.jaccard_score >= 0.5


def test_different_subject_passthrough(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.85")
    _ins(
        conn,
        text="alice runs the linux kernel patches in production for storage",
        status="confirmed",
    )
    candidate = _ins(
        conn,
        text="bob enjoys windows powershell automation in his daily workflow",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=candidate,
        candidate_text="bob enjoys windows powershell automation in his daily workflow",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"


def test_cross_scope_isolation(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.1")
    _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        scope="project:other",
        status="confirmed",
    )
    candidate = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        scope="project:memorymaster",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=candidate,
        candidate_text="user prefers vim editor with vim plugins for python development",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"
    assert result.reason == "no-fts-matches"


def test_archived_claims_excluded(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.1")
    _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        status="archived",
    )
    candidate = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=candidate,
        candidate_text="user prefers vim editor with vim plugins for python development",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"


def test_threshold_too_high_passthrough(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.95")
    _ins(
        conn,
        text="user prefers vim editor with plugins for python development",
        status="confirmed",
    )
    candidate = _ins(
        conn,
        text="user prefers emacs editor with plugins for ruby scripting",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=candidate,
        candidate_text="user prefers emacs editor with plugins for ruby scripting",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"
    assert result.jaccard_score is not None
    assert result.jaccard_score < 0.95


def test_candidate_vs_candidate_can_match(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.1")
    older = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        status="candidate",
    )
    newer = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=newer,
        candidate_text="user prefers vim editor with vim plugins for python development",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "archive"
    assert result.canonical_claim_id == older


def test_fts_candidates_returns_matches(conn: sqlite3.Connection) -> None:
    _ins(
        conn,
        text="memorymaster steward processes candidates via llm extraction",
    )
    candidate = _ins(
        conn,
        text="memorymaster steward processes candidates via llm extraction",
        status="candidate",
    )
    rows = fts_candidates_in_scope(
        conn,
        scope="project:memorymaster",
        text="memorymaster steward processes candidates via llm extraction",
        exclude_id=candidate,
        limit=5,
    )
    assert rows
    assert len(rows[0]) == 3


def test_jaccard_known_values() -> None:
    from memorymaster.govern.candidate_dedupe import jaccard_tokens
    assert jaccard_tokens("the cat sat", "the cat sat") == pytest.approx(1.0)
    assert jaccard_tokens("the cat sat", "the dog sat") == pytest.approx(2 / 4)
    assert jaccard_tokens("alpha beta gamma", "delta epsilon zeta") == 0.0
    assert jaccard_tokens("", "") == 0.0


def test_dedupe_result_is_immutable() -> None:
    r = DedupeResult(action="passthrough", canonical_claim_id=None,
                     jaccard_score=None, reason="test")
    with pytest.raises(Exception):
        r.action = "archive"  # type: ignore[misc]
