"""Tests for entity graph extraction and storage."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memorymaster.entity_graph import EntityGraph, _llm_chat, _parse_json


class TestParseJson:
    """Test JSON parsing from LLM output."""

    def test_parse_valid_json(self):
        """Parse valid JSON."""
        raw = '{"entities": [{"name": "Alice", "type": "person"}], "relations": []}'
        result = _parse_json(raw)
        assert result["entities"][0]["name"] == "Alice"

    def test_parse_json_with_markdown_fence(self):
        """Parse JSON wrapped in markdown fences."""
        raw = '```json\n{"entities": [], "relations": []}\n```'
        result = _parse_json(raw)
        assert result == {"entities": [], "relations": []}

    def test_parse_invalid_json_returns_empty(self):
        """Invalid JSON returns empty structure."""
        result = _parse_json("not json")
        assert result == {"entities": [], "relations": []}


class TestLlmChat:
    """Test LLM chat wrapper."""

    @patch("memorymaster.entity_graph.urllib.request.urlopen")
    def test_llm_chat_success(self, mock_urlopen):
        """Successful LLM call returns content."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "message": {"content": '{"entities": [], "relations": []}'}
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = _llm_chat("test query", system="test")
        assert result == '{"entities": [], "relations": []}'

    @patch("memorymaster.entity_graph.urllib.request.urlopen")
    def test_llm_chat_timeout_returns_empty(self, mock_urlopen):
        """Timeout returns empty string."""
        mock_urlopen.side_effect = TimeoutError()
        result = _llm_chat("test")
        assert result == ""


class TestEntityGraph:
    """Test EntityGraph class."""

    @pytest.fixture
    def db_path(self):
        """Create temporary database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "test.db"

    @pytest.fixture
    def graph(self, db_path):
        """Create EntityGraph instance."""
        return EntityGraph(str(db_path))

    def test_ensure_tables_creates_schema(self, graph):
        """ensure_tables creates required tables."""
        graph.ensure_tables()
        conn = graph._connect()
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [r[0] for r in tables]
            assert "entities" in table_names
            assert "entity_edges" in table_names
            assert "claim_entity_links" in table_names
        finally:
            conn.close()

    def test_ensure_tables_idempotent(self, graph):
        """ensure_tables can be called multiple times."""
        graph.ensure_tables()
        graph.ensure_tables()  # Should not error
        conn = graph._connect()
        try:
            result = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            assert result == 0
        finally:
            conn.close()

    @patch("memorymaster.entity_graph._llm_chat")
    def test_extract_and_link_empty_response(self, mock_llm, graph):
        """extract_and_link handles empty LLM response."""
        graph.ensure_tables()
        mock_llm.return_value = ""
        result = graph.extract_and_link(claim_id=1, text="test text")
        assert result == []

    @patch("memorymaster.entity_graph._llm_chat")
    def test_extract_and_link_with_entities(self, mock_llm, graph):
        """extract_and_link extracts and links entities."""
        graph.ensure_tables()
        mock_llm.return_value = json.dumps({
            "entities": [
                {"name": "Alice", "type": "person", "aliases": ["Ally"]},
                {"name": "Company X", "type": "org", "aliases": []},
            ],
            "relations": [
                {"source": "Alice", "target": "Company X", "relation": "works_at"}
            ]
        })

        result = graph.extract_and_link(claim_id=42, text="Alice works at Company X")
        assert "Alice" in result
        assert "Company X" in result

        # Verify entities were stored
        conn = graph._connect()
        try:
            entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            assert entity_count == 2

            edge_count = conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0]
            assert edge_count == 1

            link_count = conn.execute("SELECT COUNT(*) FROM claim_entity_links").fetchone()[0]
            assert link_count == 2
        finally:
            conn.close()

    @patch("memorymaster.entity_graph._llm_chat")
    def test_extract_and_link_skips_short_names(self, mock_llm, graph):
        """extract_and_link skips entity names with length < 2."""
        graph.ensure_tables()
        mock_llm.return_value = json.dumps({
            "entities": [
                {"name": "A", "type": "person", "aliases": []},  # Should skip
                {"name": "Bob", "type": "person", "aliases": []},
            ],
            "relations": []
        })

        result = graph.extract_and_link(claim_id=1, text="test")
        assert "Bob" in result
        assert "A" not in result

    def test_find_related_claims_empty(self, graph):
        """find_related_claims returns empty list for unknown entities."""
        graph.ensure_tables()
        result = graph.find_related_claims(["Unknown Entity"])
        assert result == []

    @patch("memorymaster.entity_graph._llm_chat")
    def test_find_related_claims_with_data(self, mock_llm, graph):
        """find_related_claims finds claims via entity relationships."""
        graph.ensure_tables()

        # Extract entities for claim 1
        mock_llm.return_value = json.dumps({
            "entities": [{"name": "Alice", "type": "person", "aliases": []}],
            "relations": []
        })
        graph.extract_and_link(claim_id=1, text="Alice")

        # Extract entities for claim 2 with edge back to Alice
        mock_llm.return_value = json.dumps({
            "entities": [{"name": "Bob", "type": "person", "aliases": []}],
            "relations": [{"source": "Bob", "target": "Alice", "relation": "knows"}]
        })
        graph.extract_and_link(claim_id=2, text="Bob")

        # Query for Alice's related claims
        result = graph.find_related_claims(["Alice"], hops=1, limit=10)
        assert 1 in result or 2 in result

    def test_get_stats_empty_graph(self, graph):
        """get_stats returns zeros for empty graph."""
        graph.ensure_tables()
        stats = graph.get_stats()
        assert stats["entities"] == 0
        assert stats["edges"] == 0
        assert stats["claim_links"] == 0

    @patch("memorymaster.entity_graph._llm_chat")
    def test_get_stats_with_data(self, mock_llm, graph):
        """get_stats returns accurate counts."""
        graph.ensure_tables()
        mock_llm.return_value = json.dumps({
            "entities": [
                {"name": "Alice", "type": "person", "aliases": []},
                {"name": "Bob", "type": "person", "aliases": []},
            ],
            "relations": [{"source": "Alice", "target": "Bob", "relation": "knows"}]
        })
        graph.extract_and_link(claim_id=1, text="test")

        stats = graph.get_stats()
        assert stats["entities"] == 2
        assert stats["edges"] == 1
        assert stats["claim_links"] == 2
        assert "person" in stats["by_type"]
