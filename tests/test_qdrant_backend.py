"""Tests for memorymaster.qdrant_backend (unit-level, no live Qdrant)."""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from memorymaster.models import Claim
from memorymaster.qdrant_backend import QdrantBackend, EMBEDDING_DIMS


def _fake_claim(**overrides) -> Claim:
    defaults = dict(
        id=1,
        text="Python is great",
        idempotency_key=None,
        normalized_text=None,
        claim_type="fact",
        subject="Python",
        predicate="is",
        object_value="great",
        scope="project",
        volatility="medium",
        status="confirmed",
        confidence=0.85,
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        last_validated_at=None,
        archived_at=None,
    )
    defaults.update(overrides)
    return Claim(**defaults)


class TestClaimPayload:
    def test_claim_text_concatenation(self):
        claim = _fake_claim(subject="Python", predicate="is", object_value="great", text="Python is great")
        text = QdrantBackend._claim_text(claim)
        assert "Python" in text
        assert "is" in text
        assert "great" in text

    def test_claim_payload_structure(self):
        claim = _fake_claim()
        payload = QdrantBackend._claim_payload(claim, source="test")
        assert payload["claim_id"] == 1
        assert payload["state"] == "confirmed"
        assert payload["confidence"] == 0.85
        assert payload["source"] == "test"
        assert payload["workspace"] == "main"

    def test_point_id_is_deterministic(self):
        id1 = QdrantBackend._point_id(42)
        id2 = QdrantBackend._point_id(42)
        assert id1 == id2
        # Must be valid UUID
        uuid.UUID(id1)

    def test_point_id_differs_for_different_claims(self):
        assert QdrantBackend._point_id(1) != QdrantBackend._point_id(2)


class TestEmbedFailure:
    """When Ollama is unreachable, operations should fail gracefully."""

    def test_upsert_returns_false_on_embed_failure(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        # Simulate embed failure
        backend._client.post.side_effect = Exception("connection refused")
        claim = _fake_claim()
        assert backend.upsert_claim(claim) is False

    def test_search_returns_empty_on_embed_failure(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        backend._client.post.side_effect = Exception("connection refused")
        assert backend.search("test query") == []

    def test_delete_returns_false_on_failure(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        backend._client.post.side_effect = Exception("connection refused")
        assert backend.delete_claim(1) is False


class TestEmbedSuccess:
    """Test _embed with mocked httpx responses."""

    def _make_backend(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        return backend

    def _mock_embed_response(self, client_mock, dims=EMBEDDING_DIMS):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"embeddings": [[0.1] * dims]}
        client_mock.post.return_value = resp
        return resp

    def test_embed_returns_vector(self):
        backend = self._make_backend()
        self._mock_embed_response(backend._client)
        vec = backend._embed("test text")
        assert vec is not None
        assert len(vec) == EMBEDDING_DIMS

    def test_embed_wrong_dims_returns_none(self):
        backend = self._make_backend()
        self._mock_embed_response(backend._client, dims=768)  # wrong dims
        vec = backend._embed("test text")
        assert vec is None

    def test_embed_empty_embeddings_returns_none(self):
        backend = self._make_backend()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"embeddings": []}
        backend._client.post.return_value = resp
        vec = backend._embed("test text")
        assert vec is None


class TestUpsertSuccess:
    """Test upsert_claim with successful embed + Qdrant response."""

    def _make_backend_with_embed(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        # First post = embed, second put = qdrant upsert
        embed_resp = MagicMock()
        embed_resp.status_code = 200
        embed_resp.raise_for_status = MagicMock()
        embed_resp.json.return_value = {"embeddings": [[0.1] * EMBEDDING_DIMS]}

        upsert_resp = MagicMock()
        upsert_resp.status_code = 200
        upsert_resp.raise_for_status = MagicMock()

        backend._client.post.return_value = embed_resp
        backend._client.put.return_value = upsert_resp
        return backend

    def test_upsert_returns_true(self):
        backend = self._make_backend_with_embed()
        claim = _fake_claim()
        assert backend.upsert_claim(claim) is True

    def test_upsert_calls_qdrant_put(self):
        backend = self._make_backend_with_embed()
        claim = _fake_claim()
        backend.upsert_claim(claim)
        backend._client.put.assert_called_once()
        url = backend._client.put.call_args[0][0]
        assert "/collections/agent-memories/points" in url

    def test_upsert_qdrant_failure_returns_false(self):
        backend = self._make_backend_with_embed()
        backend._client.put.side_effect = Exception("qdrant down")
        claim = _fake_claim()
        assert backend.upsert_claim(claim) is False


class TestDeleteSuccess:
    def test_delete_returns_true(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        backend._client.post.return_value = resp
        assert backend.delete_claim(1) is True

    def test_delete_calls_correct_url(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        backend._client.post.return_value = resp
        backend.delete_claim(42)
        url = backend._client.post.call_args[0][0]
        assert "/points/delete" in url


class TestSearchSuccess:
    def _make_backend_with_search(self, results=None):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()

        embed_resp = MagicMock()
        embed_resp.status_code = 200
        embed_resp.raise_for_status = MagicMock()
        embed_resp.json.return_value = {"embeddings": [[0.1] * EMBEDDING_DIMS]}

        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.raise_for_status = MagicMock()
        search_resp.json.return_value = {"result": results or []}

        # post is used for both embed and search
        backend._client.post.side_effect = [embed_resp, search_resp]
        return backend

    def test_search_returns_results(self):
        hits = [{"payload": {"claim_id": 1}, "score": 0.95}]
        backend = self._make_backend_with_search(hits)
        results = backend.search("test query")
        assert len(results) == 1
        assert results[0]["claim_id"] == 1
        assert results[0]["score"] == 0.95

    def test_search_empty_results(self):
        backend = self._make_backend_with_search([])
        assert backend.search("nothing") == []

    def test_search_with_filters(self):
        backend = self._make_backend_with_search([])
        backend.search("test", states=["confirmed"], min_confidence=0.5)
        # Second post call is the search — check the body has filters
        search_call = backend._client.post.call_args_list[1]
        body = search_call[1]["json"]
        assert "filter" in body

    def test_search_qdrant_failure_returns_empty(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        embed_resp = MagicMock()
        embed_resp.status_code = 200
        embed_resp.raise_for_status = MagicMock()
        embed_resp.json.return_value = {"embeddings": [[0.1] * EMBEDDING_DIMS]}
        backend._client.post.side_effect = [embed_resp, Exception("qdrant down")]
        assert backend.search("test") == []


class TestEnsureCollection:
    def test_existing_collection_skips_create(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        backend._client.get.return_value = resp
        backend.ensure_collection()
        backend._client.put.assert_not_called()

    def test_missing_collection_creates(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        get_resp = MagicMock()
        get_resp.status_code = 404
        backend._client.get.return_value = get_resp
        put_resp = MagicMock()
        put_resp.status_code = 200
        put_resp.raise_for_status = MagicMock()
        backend._client.put.return_value = put_resp
        backend.ensure_collection()
        backend._client.put.assert_called_once()


class TestSyncAll:
    def test_sync_all_counts(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        # Mock ensure_collection
        get_resp = MagicMock()
        get_resp.status_code = 200
        backend._client.get.return_value = get_resp

        # Mock embed + upsert
        embed_resp = MagicMock()
        embed_resp.status_code = 200
        embed_resp.raise_for_status = MagicMock()
        embed_resp.json.return_value = {"embeddings": [[0.1] * EMBEDDING_DIMS]}
        backend._client.post.return_value = embed_resp

        upsert_resp = MagicMock()
        upsert_resp.status_code = 200
        upsert_resp.raise_for_status = MagicMock()
        backend._client.put.return_value = upsert_resp

        # Mock store
        store = MagicMock()
        claims = [_fake_claim(id=i) for i in range(3)]
        store.find_by_status.return_value = claims

        result = backend.sync_all(store)
        assert result["total"] == 12  # 3 claims * 4 statuses
        assert result["synced"] == 12
        assert result["errors"] == 0


class TestClose:
    def test_close_calls_client_close(self):
        backend = QdrantBackend(qdrant_url="http://localhost:1", ollama_url="http://localhost:2")
        backend._client = MagicMock()
        backend.close()
        backend._client.close.assert_called_once()


class TestServiceQdrantIntegration:
    """Test that the service layer handles qdrant being None gracefully."""

    def test_qdrant_sync_noop_when_none(self):
        """_qdrant_sync should not raise when qdrant is None."""
        from memorymaster.service import MemoryService
        # Patch create_store and create_best_provider to avoid DB
        with patch("memorymaster.service.create_store"), \
             patch("memorymaster.service.create_best_provider"):
            svc = MemoryService.__new__(MemoryService)
            svc.qdrant = None
            claim = _fake_claim()
            # Should not raise
            svc._qdrant_sync(claim)
