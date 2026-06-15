from __future__ import annotations

import sqlite3

from memorymaster.recall import verbatim_store


def test_hybrid_search_keeps_same_prefix_distinct_content(tmp_path, monkeypatch):
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

    vector_results = [
        {
            "session_id": "session",
            "role": "user",
            "content": content,
            "scope": "project:test",
            "score": 1.0 - (idx * 0.01),
            "source": "vector",
        }
        for idx, content in enumerate(contents)
    ]
    monkeypatch.setattr(verbatim_store, "_search_vector", lambda *args: vector_results)

    results = verbatim_store.search_verbatim(
        str(db_path),
        "zz-no-fts-match",
        scope="project:test",
        limit=3,
        mode="hybrid",
    )

    assert [r["content"] for r in results] == contents
