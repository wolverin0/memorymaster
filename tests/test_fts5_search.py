"""Tests for FTS5 full-text search on claims."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from memorymaster.models import CitationInput
from memorymaster.storage import SQLiteStore


def _tmp_db(prefix: str = "fts5") -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


@pytest.fixture()
def store() -> SQLiteStore:
    db = _tmp_db()
    s = SQLiteStore(db)
    s.init_db()
    return s


def _cite(label: str = "test") -> list[CitationInput]:
    return [CitationInput(source="test://src", locator=label, excerpt=label)]


class TestFTS5TableCreation:
    def test_fts5_table_exists_after_init(self, store: SQLiteStore) -> None:
        with store.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims_fts'"
            ).fetchone()
        assert row is not None

    def test_init_db_is_idempotent(self, store: SQLiteStore) -> None:
        store.init_db()
        store.init_db()
        with store.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims_fts'"
            ).fetchone()
        assert row is not None


class TestFTS5Search:
    def test_basic_text_match(self, store: SQLiteStore) -> None:
        store.create_claim("Server IP is 10.0.0.1", _cite(), subject="server", predicate="ip")
        store.create_claim("Database runs on port 5432", _cite(), subject="db", predicate="port")

        results = store.list_claims(text_query="server IP")
        assert len(results) == 1
        assert "10.0.0.1" in results[0].text

    def test_single_token_match(self, store: SQLiteStore) -> None:
        store.create_claim("The authentication token expires daily", _cite())
        store.create_claim("Backup runs every night", _cite())

        results = store.list_claims(text_query="authentication")
        assert len(results) == 1
        assert "authentication" in results[0].text

    def test_no_results_for_nonexistent_term(self, store: SQLiteStore) -> None:
        store.create_claim("Sample claim text here", _cite())
        results = store.list_claims(text_query="zzzznonexistent999")
        assert len(results) == 0

    def test_search_with_status_filter(self, store: SQLiteStore) -> None:
        store.create_claim("Alpha feature flag is enabled", _cite())
        results = store.list_claims(text_query="Alpha", status="candidate")
        assert len(results) == 1

        results = store.list_claims(text_query="Alpha", status="confirmed")
        assert len(results) == 0

    def test_search_with_status_in_filter(self, store: SQLiteStore) -> None:
        store.create_claim("Beta release scheduled", _cite())
        results = store.list_claims(text_query="Beta", status_in=["candidate", "confirmed"])
        assert len(results) == 1

    def test_search_subject_and_predicate(self, store: SQLiteStore) -> None:
        store.create_claim(
            "VM credentials stored securely",
            _cite(),
            subject="infrastructure",
            predicate="credential_storage",
            object_value="vault",
        )
        results = store.list_claims(text_query="infrastructure")
        assert len(results) == 1

    def test_search_after_update_reflects_new_text(self, store: SQLiteStore) -> None:
        claim = store.create_claim("Original text here", _cite(), subject="test", predicate="value")

        store.redact_claim_payload(claim.id, mode="redact")

        results = store.list_claims(text_query="Original")
        assert len(results) == 0

        results = store.list_claims(text_query="REDACTED")
        assert len(results) >= 1

    def test_search_respects_limit(self, store: SQLiteStore) -> None:
        for i in range(5):
            store.create_claim(f"Common keyword item {i}", _cite(f"t{i}"))

        results = store.list_claims(text_query="Common keyword", limit=3)
        assert len(results) == 3

    def test_special_characters_in_query(self, store: SQLiteStore) -> None:
        store.create_claim("Path is C:\\Users\\test\\file.txt", _cite())
        results = store.list_claims(text_query="C:\\Users")
        assert len(results) >= 0  # should not crash

    def test_empty_query_returns_all(self, store: SQLiteStore) -> None:
        store.create_claim("First claim", _cite())
        store.create_claim("Second claim", _cite())

        results = store.list_claims(text_query="", limit=10)
        assert len(results) == 2

        results = store.list_claims(text_query=None, limit=10)
        assert len(results) == 2


class TestFTS5Backfill:
    def test_existing_claims_indexed_after_reinit(self) -> None:
        db = _tmp_db("backfill")
        store = SQLiteStore(db)
        store.init_db()

        store.create_claim("Pre-existing claim about deployment", _cite())

        # Drop and recreate FTS table to simulate migration
        with store.connect() as conn:
            conn.execute("DROP TABLE IF EXISTS claims_fts")
            conn.execute("DROP TRIGGER IF EXISTS trg_claims_fts_insert")
            conn.execute("DROP TRIGGER IF EXISTS trg_claims_fts_update")
            conn.execute("DROP TRIGGER IF EXISTS trg_claims_fts_delete")
            conn.commit()

        store.init_db()

        results = store.list_claims(text_query="deployment")
        assert len(results) == 1


class TestFTS5EscapeQuery:
    def test_escape_simple(self) -> None:
        assert SQLiteStore._escape_fts5_query("hello world") == '"hello" "world"'

    def test_escape_special_chars(self) -> None:
        result = SQLiteStore._escape_fts5_query('test "quoted" value')
        assert '""' in result  # double quotes escaped

    def test_escape_empty(self) -> None:
        assert SQLiteStore._escape_fts5_query("") == '""'

    def test_escape_fts5_operators(self) -> None:
        result = SQLiteStore._escape_fts5_query("NOT AND OR")
        assert '"NOT"' in result
        assert '"AND"' in result
        assert '"OR"' in result
