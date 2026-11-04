from __future__ import annotations

import sqlite3

import pytest

from memorymaster.recall import verbatim_store


def test_hybrid_search_uses_authoritative_fts_during_quarantine(tmp_path, monkeypatch):
    db_path = tmp_path / "verbatim.db"
    prefix = "x" * 100
    contents = [f"{prefix} distinct suffix {idx}" for idx in range(3)]

    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE verbatim_memories (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            scope TEXT,
            timestamp TEXT,
            source_agent TEXT,
            embedding_synced INTEGER DEFAULT 0
        )"""
    )
    conn.execute("CREATE VIRTUAL TABLE verbatim_fts USING fts5(content)")
    for idx, content in enumerate(contents, start=1):
        conn.execute(
            """INSERT INTO verbatim_memories
               (id, session_id, role, content, scope, timestamp, source_agent)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                idx,
                "session",
                "user",
                content,
                "project:test",
                "2026-05-11T00:00:00Z",
                "test",
            ),
        )
        conn.execute("INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)", (idx, content))
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        verbatim_store,
        "_search_vector",
        lambda *args: pytest.fail("quarantined vector adapter was called"),
    )

    results = verbatim_store.search_verbatim(
        str(db_path),
        "zz-no-fts-match",
        scope="project:test",
        limit=3,
        mode="hybrid",
    )

    assert results == []


def test_freshness_reports_the_newest_row_and_flags_staleness(tmp_path) -> None:
    """T-2116: an empty search must be distinguishable from storage having stopped."""
    import sqlite3
    from datetime import datetime, timezone

    db = tmp_path / "v.db"
    verbatim_store.ensure_verbatim_schema(str(db))
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO verbatim_memories (session_id, role, content, timestamp) "
                 "VALUES ('s', 'user', 'old turn', '2026-08-24T15:49:21.754968+00:00')")
    conn.commit()
    conn.close()

    stale = verbatim_store.verbatim_freshness(str(db), now=datetime(2026, 11, 3, tzinfo=timezone.utc))
    fresh = verbatim_store.verbatim_freshness(str(db), now=datetime(2026, 8, 26, tzinfo=timezone.utc))

    assert stale == {"newest_verbatim_at": "2026-08-24T15:49:21.754968+00:00", "newest_age_days": 70, "stale": True}
    assert fresh["stale"] is False
    assert verbatim_store.verbatim_freshness(str(tmp_path / "missing.db")) == {}


def test_mcp_search_adds_freshness_and_a_stale_warning_without_changing_existing_fields(tmp_path) -> None:
    import sqlite3

    from memorymaster.surfaces import mcp_server

    db = tmp_path / "v.db"
    verbatim_store.ensure_verbatim_schema(str(db))
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO verbatim_memories (session_id, role, content, timestamp) "
                 "VALUES ('s', 'user', 'harness notes', '2026-08-24T15:49:21+00:00')")
    conn.commit()
    conn.close()

    result = mcp_server.search_verbatim(query="DeepSeek Harness", db=str(db))

    assert result["ok"] is True and result["rows"] == 0 and result["results"] == []
    assert result["stale"] is True and result["newest_age_days"] > 7
    assert "stale" in result["warning"] and "transcript-search" in result["warning"]
