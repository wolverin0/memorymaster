"""Tests for vector search / embedding integration.

These tests verify the embedding provider hierarchy, vector scoring,
hybrid retrieval with semantic weights, and graceful degradation
when optional dependencies are not installed.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memorymaster.recall.embeddings import (
    EmbeddingProvider,
    cosine_similarity,
    create_best_provider,
    hash_embed,
    normalize,
)
from memorymaster.core.models import Claim
from memorymaster.stores.storage import SQLiteStore

# ML/torch tests: loads real sentence-transformers/Qdrant paths; excluded from
# the default run (see pytest.ini). Run in isolation with: pytest -m ml
pytestmark = pytest.mark.ml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_claim(
    claim_id: int,
    text: str,
    *,
    status: str = "confirmed",
    confidence: float = 0.8,
    subject: str | None = None,
    pinned: bool = False,
) -> Claim:
    return Claim(
        id=claim_id,
        text=text,
        idempotency_key=None,
        normalized_text=None,
        claim_type=None,
        subject=subject,
        predicate=None,
        object_value=None,
        scope="project",
        volatility="medium",
        status=status,
        confidence=confidence,
        pinned=pinned,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-03-01T00:00:00+00:00",
        updated_at="2026-03-08T00:00:00+00:00",
        last_validated_at=None,
        archived_at=None,
    )


# ---------------------------------------------------------------------------
# EmbeddingProvider basics
# ---------------------------------------------------------------------------

class TestEmbeddingProvider:
    def test_hash_provider_is_not_semantic(self) -> None:
        provider = EmbeddingProvider(model="hash-v1", dims=128)
        assert not provider.is_semantic

    def test_semantic_model_is_semantic(self) -> None:
        provider = EmbeddingProvider(model="all-MiniLM-L6-v2", dims=384)
        assert provider.is_semantic

    def test_gemini_model_is_semantic(self) -> None:
        provider = EmbeddingProvider(model="gemini:text-embedding-004", dims=768)
        assert provider.is_semantic

    def test_hash_embed_deterministic(self) -> None:
        vec1 = hash_embed("hello world", dims=64)
        vec2 = hash_embed("hello world", dims=64)
        assert vec1 == vec2

    def test_hash_embed_different_texts_differ(self) -> None:
        vec1 = hash_embed("authentication via JWT", dims=64)
        vec2 = hash_embed("database migration scripts", dims=64)
        sim = cosine_similarity(vec1, vec2)
        # Hash embeddings are not semantic but different texts should differ
        assert sim < 0.99

    def test_hash_embed_normalized(self) -> None:
        vec = hash_embed("test normalization", dims=128)
        mag = math.sqrt(sum(v * v for v in vec))
        assert abs(mag - 1.0) < 1e-6

    def test_hash_embed_empty_text(self) -> None:
        vec = hash_embed("", dims=64)
        assert all(v == 0.0 for v in vec)
        assert len(vec) == 64

    def test_hash_embed_invalid_dims(self) -> None:
        with pytest.raises(ValueError, match="dims must be > 0"):
            hash_embed("text", dims=0)

    def test_embed_dispatches_to_hash(self) -> None:
        provider = EmbeddingProvider(model="hash-v1", dims=64)
        vec = provider.embed("test")
        assert len(vec) == 64
        assert vec == hash_embed("test", dims=64)


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        vec = normalize([1.0, 2.0, 3.0])
        assert cosine_similarity(vec, vec) == pytest.approx(1.0, abs=1e-6)

    def test_opposite_vectors(self) -> None:
        vec = normalize([1.0, 0.0, 0.0])
        neg = normalize([-1.0, 0.0, 0.0])
        assert cosine_similarity(vec, neg) == pytest.approx(-1.0, abs=1e-6)

    def test_orthogonal_vectors(self) -> None:
        a = normalize([1.0, 0.0])
        b = normalize([0.0, 1.0])
        assert cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-6)

    def test_dimension_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="same dimension"):
            cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])


# ---------------------------------------------------------------------------
# create_best_provider fallback chain
# ---------------------------------------------------------------------------

class TestCreateBestProvider:
    def test_falls_back_to_hash_when_nothing_available(self) -> None:
        with patch("memorymaster.recall.embeddings._load_transformer", side_effect=ImportError("no ST")), \
             patch.dict("os.environ", {"GEMINI_API_KEY": "", "OPENAI_API_KEY": ""}, clear=False):
            provider = create_best_provider()
        assert provider.model == "hash-v1"
        assert not provider.is_semantic

    def test_prefers_sentence_transformers(self) -> None:
        mock_transformer = MagicMock()
        mock_transformer.get_sentence_embedding_dimension.return_value = 384
        with patch("memorymaster.recall.embeddings._load_transformer", return_value=mock_transformer):
            provider = create_best_provider()
        assert provider.model == "all-MiniLM-L6-v2"
        assert provider.is_semantic
        assert provider.dims == 384

    def test_falls_back_to_gemini_when_st_unavailable(self) -> None:
        mock_client = MagicMock()
        with (
            patch("memorymaster.recall.embeddings._load_transformer", side_effect=ImportError("no ST")),
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
            patch("memorymaster.recall.embeddings._load_gemini_client", return_value=mock_client),
        ):
            provider = create_best_provider()
        # Default upgraded to gemini-embedding-001 after Google deprecated
        # text-embedding-004 from v1beta (see memorymaster/embeddings.py).
        assert provider.model == "gemini:gemini-embedding-001"
        assert provider.is_semantic

    def test_no_gemini_without_api_key(self) -> None:
        with (
            patch("memorymaster.recall.embeddings._load_transformer", side_effect=ImportError("no ST")),
            patch.dict("os.environ", {}, clear=True),
        ):
            # Remove both possible env vars
            import os
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            provider = create_best_provider()
        assert provider.model == "hash-v1"


# ---------------------------------------------------------------------------
# SQLiteStore embedding/vector methods
# ---------------------------------------------------------------------------

class TestStoreVectorScores:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> SQLiteStore:
        db = tmp_path / "test.db"
        s = SQLiteStore(str(db))
        s.init_db()
        return s

    @pytest.fixture()
    def provider(self) -> EmbeddingProvider:
        return EmbeddingProvider(model="hash-v1", dims=64)

    def _insert_claim(self, store: SQLiteStore, text: str) -> Claim:
        from memorymaster.core.models import CitationInput
        return store.create_claim(
            text=text,
            citations=[CitationInput(source="test")],
        )

    def test_upsert_and_retrieve(self, store: SQLiteStore, provider: EmbeddingProvider) -> None:
        claim = self._insert_claim(store, "JWT authentication flow")
        count = store.upsert_embeddings([claim], provider)
        assert count == 1

        # Verify stored in DB
        with store.connect() as conn:
            row = conn.execute(
                "SELECT model, embedding_json FROM claim_embeddings WHERE claim_id = ?",
                (claim.id,),
            ).fetchone()
        assert row is not None
        assert row["model"] == "hash-v1"
        emb = json.loads(row["embedding_json"])
        assert len(emb) == 64

    def test_vector_scores_returns_similarities(self, store: SQLiteStore, provider: EmbeddingProvider) -> None:
        c1 = self._insert_claim(store, "JWT authentication flow")
        c2 = self._insert_claim(store, "database migration strategy")
        scores = store.vector_scores("authentication", [c1, c2], provider)
        assert c1.id in scores
        assert c2.id in scores
        # Scores should be in [0, 1] (normalized from [-1,1])
        for score in scores.values():
            assert 0.0 <= score <= 1.0

    def test_vector_scores_empty_claims(self, store: SQLiteStore, provider: EmbeddingProvider) -> None:
        result = store.vector_scores("query", [], provider)
        assert result == {}

    def test_upsert_idempotent(self, store: SQLiteStore, provider: EmbeddingProvider) -> None:
        claim = self._insert_claim(store, "some text")
        store.upsert_embeddings([claim], provider)
        store.upsert_embeddings([claim], provider)

        with store.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM claim_embeddings WHERE claim_id = ?",
                (claim.id,),
            ).fetchone()["cnt"]
        assert count == 1


# ---------------------------------------------------------------------------
# Hybrid retrieval with semantic_vectors flag
# ---------------------------------------------------------------------------
