"""Tests for memorymaster.embeddings — coverage gaps (provider creation, fallbacks)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from memorymaster.embeddings import (
    EmbeddingProvider,
    _load_gemini_client,
    _load_transformer,
    cosine_similarity,
    create_best_provider,
    normalize,
)


class TestEmbeddingProviderEmbed:
    def test_hash_model(self):
        p = EmbeddingProvider(model="hash-v1", dims=128)
        vec = p.embed("hello world")
        assert len(vec) == 128
        assert p.is_semantic is False

    def test_gemini_model_calls_gemini_embed(self):
        p = EmbeddingProvider(model="gemini:text-embedding-004", dims=768)
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_embedding = MagicMock()
        mock_embedding.values = [0.1] * 768
        mock_result.embeddings = [mock_embedding]
        mock_client.models.embed_content.return_value = mock_result
        p._transformer = mock_client
        vec = p.embed("test")
        assert len(vec) == 768
        assert p.is_semantic is True

    def test_semantic_model_calls_transformer(self):
        np = pytest.importorskip("numpy")
        p = EmbeddingProvider(model="all-MiniLM-L6-v2", dims=384)
        mock_transformer = MagicMock()
        mock_transformer.encode.return_value = np.array([0.1] * 384)
        p._transformer = mock_transformer
        vec = p.embed("test")
        assert len(vec) == 384
        assert p.is_semantic is True


class TestLoadTransformer:
    def test_import_error(self):
        with patch.dict("sys.modules", {"sentence_transformers": None}):
            with pytest.raises(ImportError, match="sentence-transformers"):
                _load_transformer("all-MiniLM-L6-v2")


class TestLoadGeminiClient:
    def test_no_key_raises(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            with pytest.raises(ValueError, match="API key required"):
                _load_gemini_client()

    def test_import_error(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            with patch.dict("sys.modules", {"google": None, "google.genai": None}):
                with pytest.raises((ImportError, ModuleNotFoundError)):
                    _load_gemini_client(api_key="test-key")


class TestCreateBestProvider:
    def test_fallback_to_hash(self):
        """When no providers are available, falls back to hash-v1."""
        with patch("memorymaster.embeddings.create_semantic_provider", side_effect=ImportError):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GEMINI_API_KEY", None)
                os.environ.pop("GOOGLE_API_KEY", None)
                p = create_best_provider()
                assert p.model == "hash-v1"

    def test_semantic_failure_falls_through(self):
        """Non-import failure from sentence-transformers falls to next."""
        with patch("memorymaster.embeddings.create_semantic_provider", side_effect=RuntimeError("broken")):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GEMINI_API_KEY", None)
                os.environ.pop("GOOGLE_API_KEY", None)
                p = create_best_provider()
                assert p.model == "hash-v1"

    def test_gemini_tried_when_key_present(self):
        """Gemini is attempted when API key is set."""
        with patch("memorymaster.embeddings.create_semantic_provider", side_effect=ImportError):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}):
                with patch("memorymaster.embeddings.create_gemini_provider", side_effect=ImportError):
                    p = create_best_provider()
                    assert p.model == "hash-v1"

    def test_gemini_failure_falls_to_hash(self):
        with patch("memorymaster.embeddings.create_semantic_provider", side_effect=ImportError):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}):
                with patch("memorymaster.embeddings.create_gemini_provider", side_effect=RuntimeError("fail")):
                    p = create_best_provider()
                    assert p.model == "hash-v1"


class TestNormalize:
    def test_zero_vector_unchanged(self):
        result = normalize([0.0, 0.0, 0.0])
        assert result == [0.0, 0.0, 0.0]

    def test_normalized_unit_length(self):
        import math
        result = normalize([3.0, 4.0])
        mag = math.sqrt(sum(v * v for v in result))
        assert abs(mag - 1.0) < 1e-6


class TestCosine:
    def test_same_vector(self):
        v = normalize([1.0, 2.0, 3.0])
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-6

    def test_different_dims_raises(self):
        with pytest.raises(ValueError, match="same dimension"):
            cosine_similarity([1.0], [1.0, 2.0])
