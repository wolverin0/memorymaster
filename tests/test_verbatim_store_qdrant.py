from __future__ import annotations

import hashlib
import json
import sqlite3

from memorymaster.recall import verbatim_store

import pytest

# ML/torch tests: loads real sentence-transformers/Qdrant paths; excluded from
# the default run (see pytest.ini). Run in isolation with: pytest -m ml
pytestmark = pytest.mark.ml


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def _create_verbatim_db(db_path):
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
    conn.commit()
    conn.close()


def test_hybrid_search_does_not_read_qdrant_payloads_during_quarantine(tmp_path, monkeypatch):
    db_path = tmp_path / "verbatim.db"
    _create_verbatim_db(db_path)

    def fake_urlopen(req, timeout):
        pytest.fail(f"quarantined search reached network: {req.full_url}")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(verbatim_store, "QDRANT_URL", "http://test-qdrant:6333")
    monkeypatch.setattr(verbatim_store.urllib.request, "urlopen", fake_urlopen)

    results = verbatim_store.search_verbatim(
        str(db_path),
        "no matching fts row",
        scope="project:test",
        limit=3,
        mode="hybrid",
    )

    assert results == []


def test_sync_to_qdrant_payload_uses_full_content_hash(tmp_path, monkeypatch):
    db_path = tmp_path / "verbatim.db"
    _create_verbatim_db(db_path)
    prefix = "x" * 2000
    content = f"{prefix} unique suffix"

    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO verbatim_memories
           (id, session_id, role, content, scope, timestamp, source_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (1, "session", "user", content, "project:test", "2026-05-11T00:00:00Z", "test"),
    )
    conn.commit()
    conn.close()

    captured_points = []

    def fake_urlopen(req, timeout):
        if req.full_url == f"{verbatim_store.QDRANT_URL}/collections/{verbatim_store.QDRANT_COLLECTION}":
            return _Response({})
        if req.full_url == "https://api.openai.com/v1/embeddings":
            return _Response({"data": [{"embedding": [0.0] * verbatim_store.EMBED_DIM}]})
        assert req.full_url.endswith("/points")
        captured_points.extend(json.loads(req.data.decode())["points"])
        return _Response({})

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(verbatim_store, "QDRANT_URL", "http://test-qdrant:6333")
    monkeypatch.setattr(verbatim_store.urllib.request, "urlopen", fake_urlopen)

    assert verbatim_store.sync_to_qdrant(str(db_path)) == {"synced": 1}
    assert captured_points[0]["payload"]["content"] == content[:2000]
    assert captured_points[0]["payload"]["content_hash"] == hashlib.sha256(content.encode()).hexdigest()


def test_qdrant_url_default_is_not_a_hardcoded_private_ip():
    """Regression: a routable private LAN IP (192.168.x) was once baked in as the
    QDRANT_URL default and shipped to PyPI. The default must be empty (vector
    disabled), never a hardcoded host. See audit verbatim-hardcoded-private-ip."""
    import re as _re

    src = (
        __import__("pathlib").Path(verbatim_store.__file__).read_text(encoding="utf-8")
    )
    # No RFC1918 literal anywhere in the module source.
    assert not _re.search(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.", src), (
        "private IP literal found in verbatim_store.py source"
    )


def test_sync_to_qdrant_disabled_when_url_unset(tmp_path, monkeypatch):
    """Empty QDRANT_URL must disable sync (like a missing OPENAI_API_KEY), not
    attempt a request against an empty host."""
    db_path = tmp_path / "verbatim.db"
    _create_verbatim_db(db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(verbatim_store, "QDRANT_URL", "")
    result = verbatim_store.sync_to_qdrant(str(db_path))
    assert result == {"synced": 0, "error": "no QDRANT_URL"}
