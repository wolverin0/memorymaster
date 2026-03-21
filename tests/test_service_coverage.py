"""Tests for memorymaster.service — coverage gaps."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memorymaster.models import CitationInput, Claim
from memorymaster.service import MemoryService


@pytest.fixture
def svc(tmp_path):
    """Create a real MemoryService with SQLite in tmp_path."""
    db = str(tmp_path / "test.db")
    s = MemoryService(db_target=db, workspace_root=tmp_path)
    s.init_db()
    return s


def _ingest(svc, text="Test claim", source="test.py"):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source=source)],
    )


class TestInitQdrant:
    def test_no_env_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QDRANT_URL", None)
            assert MemoryService._init_qdrant() is None

    def test_with_env_but_unreachable(self):
        with patch.dict(os.environ, {"QDRANT_URL": "http://localhost:1"}):
            # Should fail gracefully and return None
            result = MemoryService._init_qdrant()
            assert result is None

    def test_with_env_and_working_backend(self):
        mock_backend = MagicMock()
        with patch.dict(os.environ, {"QDRANT_URL": "http://localhost:6333"}):
            with patch("memorymaster.qdrant_backend.QdrantBackend", return_value=mock_backend):
                result = MemoryService._init_qdrant()
                assert result is mock_backend
                mock_backend.ensure_collection.assert_called_once()


class TestQdrantSync:
    def _make_svc_with_qdrant(self, tmp_path):
        svc = MemoryService(db_target=str(tmp_path / "t.db"), workspace_root=tmp_path)
        svc.init_db()
        svc.qdrant = MagicMock()
        return svc

    def test_sync_upserts_for_confirmed(self, tmp_path):
        svc = self._make_svc_with_qdrant(tmp_path)
        claim = _ingest(svc, "Test")
        # qdrant.upsert_claim should have been called
        svc.qdrant.upsert_claim.assert_called()

    def test_sync_deletes_for_archived(self, tmp_path):
        svc = self._make_svc_with_qdrant(tmp_path)
        fake_claim = MagicMock()
        fake_claim.id = 1
        fake_claim.status = "archived"
        svc._qdrant_sync(fake_claim)
        svc.qdrant.delete_claim.assert_called_once_with(1)

    def test_sync_handles_exception(self, tmp_path):
        svc = self._make_svc_with_qdrant(tmp_path)
        svc.qdrant.upsert_claim.side_effect = Exception("boom")
        fake_claim = MagicMock()
        fake_claim.id = 1
        fake_claim.status = "confirmed"
        # Should not raise
        svc._qdrant_sync(fake_claim)


class TestQdrantPostCycleSync:
    def test_post_cycle_upserts(self, tmp_path):
        svc = MemoryService(db_target=str(tmp_path / "t.db"), workspace_root=tmp_path)
        svc.init_db()
        svc.qdrant = MagicMock()
        _ingest(svc, "Claim for cycle")
        svc.run_cycle()
        # qdrant.upsert_claim should have been called during post-cycle
        assert svc.qdrant.upsert_claim.call_count >= 1

    def test_post_cycle_handles_exception(self, tmp_path):
        svc = MemoryService(db_target=str(tmp_path / "t.db"), workspace_root=tmp_path)
        svc.init_db()
        svc.qdrant = MagicMock()
        svc.qdrant.upsert_claim.side_effect = Exception("qdrant down")
        _ingest(svc, "Claim")
        # Should not raise
        svc._qdrant_post_cycle_sync()


class TestNormalizeScopeAllowlist:
    def test_none(self):
        assert MemoryService._normalize_scope_allowlist(None) is None

    def test_empty_list(self):
        assert MemoryService._normalize_scope_allowlist([]) is None

    def test_whitespace_only(self):
        assert MemoryService._normalize_scope_allowlist(["  ", ""]) is None

    def test_dedup(self):
        result = MemoryService._normalize_scope_allowlist(["a", "b", "a"])
        assert result == ["a", "b"]

    def test_strips(self):
        result = MemoryService._normalize_scope_allowlist(["  project  ", "global"])
        assert result == ["project", "global"]


class TestQueryForContext(object):
    def test_returns_context_result(self, svc):
        _ingest(svc, "Python uses indentation")
        result = svc.query_for_context(query="Python", token_budget=2000)
        assert hasattr(result, "output")
        assert hasattr(result, "claims_considered")
        assert hasattr(result, "tokens_used")

    def test_empty_db(self, svc):
        result = svc.query_for_context(query="anything")
        assert result.claims_considered == 0


class TestIngestEdgeCases:
    def test_empty_text_raises(self, svc):
        with pytest.raises(ValueError, match="empty"):
            svc.ingest(text="", citations=[CitationInput(source="x")])

    def test_whitespace_only_raises(self, svc):
        with pytest.raises(ValueError, match="empty"):
            svc.ingest(text="   ", citations=[CitationInput(source="x")])

    def test_no_citations_raises(self, svc):
        with pytest.raises(ValueError, match="citation"):
            svc.ingest(text="Valid text", citations=[])

    def test_idempotency_key_dedup(self, svc):
        c1 = svc.ingest(text="First", citations=[CitationInput(source="x")], idempotency_key="key1")
        c2 = svc.ingest(text="Second", citations=[CitationInput(source="x")], idempotency_key="key1")
        assert c1.id == c2.id  # Same claim returned


class TestPinErrors:
    def test_pin_nonexistent_raises(self, svc):
        with pytest.raises(ValueError, match="does not exist"):
            svc.pin(claim_id=99999)


class TestRedactErrors:
    def test_redact_zero_id_raises(self, svc):
        with pytest.raises(ValueError, match="positive"):
            svc.redact_claim_payload(claim_id=0)

    def test_redact_negative_id_raises(self, svc):
        with pytest.raises(ValueError, match="positive"):
            svc.redact_claim_payload(claim_id=-1)
