from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EmbeddingProvider:
    model: str = "hash-v1"
    dims: int = 1536
    _transformer: Any = field(default=None, repr=False)

    @property
    def is_semantic(self) -> bool:
        """Return True if this provider produces real semantic embeddings."""
        return not self.model.startswith("hash")

    def embed(self, text: str) -> list[float]:
        if self.model.startswith("hash"):
            return hash_embed(text, dims=self.dims)
        if self.model.startswith("gemini:"):
            return self._gemini_embed(text)
        return self._semantic_embed(text)

    def _semantic_embed(self, text: str) -> list[float]:
        if self._transformer is None:
            self._transformer = _load_transformer(self.model)
        embedding = self._transformer.encode(text, normalize_embeddings=True)
        self.dims = len(embedding)
        return embedding.tolist()

    def _gemini_embed(self, text: str) -> list[float]:
        if self._transformer is None:
            self._transformer = _load_gemini_client()
        gemini_model = self.model.split(":", 1)[1]
        result = self._transformer.models.embed_content(
            model=gemini_model,
            contents=text,
        )
        vec = result.embeddings[0].values
        self.dims = len(vec)
        return normalize(list(vec))


def create_semantic_provider(model: str = "all-MiniLM-L6-v2") -> EmbeddingProvider:
    """Create a provider using sentence-transformers for real semantic embeddings.

    Requires: pip install sentence-transformers
    Models: all-MiniLM-L6-v2 (384-dim, fast), all-mpnet-base-v2 (768-dim, better)
    """
    provider = EmbeddingProvider(model=model, dims=384)
    provider._transformer = _load_transformer(model)
    provider.dims = provider._transformer.get_sentence_embedding_dimension()
    return provider


def create_gemini_provider(
    model: str = "text-embedding-004",
    api_key: str | None = None,
) -> EmbeddingProvider:
    """Create a provider using the Gemini embedding API.

    Requires: pip install google-genai
    Set GEMINI_API_KEY or GOOGLE_API_KEY env var, or pass api_key directly.
    """
    client = _load_gemini_client(api_key=api_key)
    provider = EmbeddingProvider(model=f"gemini:{model}", dims=768)
    provider._transformer = client
    return provider


def create_best_provider() -> EmbeddingProvider:
    """Auto-detect the best available embedding provider.

    Priority:
      1. sentence-transformers (all-MiniLM-L6-v2) -- local, fast, 384-dim
      2. Gemini embedding API -- requires API key, 768-dim
      3. hash-v1 fallback -- deterministic, no semantic understanding

    Returns the best available provider without raising errors.
    """
    # 1. Try sentence-transformers
    try:
        provider = create_semantic_provider("all-MiniLM-L6-v2")
        logger.info("Using sentence-transformers (all-MiniLM-L6-v2) for embeddings")
        return provider
    except ImportError:
        logger.debug("sentence-transformers not installed, trying Gemini API")
    except Exception as exc:
        logger.warning("sentence-transformers failed to load: %s", exc)

    # 2. Try Gemini API
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_key:
        try:
            provider = create_gemini_provider(api_key=gemini_key)
            logger.info("Using Gemini embedding API for embeddings")
            return provider
        except ImportError:
            logger.debug("google-genai not installed, falling back to hash embeddings")
        except Exception as exc:
            logger.warning("Gemini embedding API failed: %s", exc)

    # 3. Fallback
    logger.info("Using hash-v1 fallback embeddings (no semantic understanding)")
    return EmbeddingProvider(model="hash-v1", dims=1536)


def _load_transformer(model: str) -> Any:
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(model)
    except ImportError as e:
        raise ImportError(
            "sentence-transformers is required for semantic embeddings. "
            "Install with: pip install sentence-transformers\n"
            "Or use the default hash-v1 model (no dependencies, but not semantic)."
        ) from e


def _load_gemini_client(api_key: str | None = None) -> Any:
    resolved_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not resolved_key:
        raise ValueError(
            "Gemini API key required. Set GEMINI_API_KEY or GOOGLE_API_KEY env var, "
            "or pass api_key directly."
        )
    try:
        from google import genai
        return genai.Client(api_key=resolved_key)
    except ImportError as e:
        raise ImportError(
            "google-genai is required for Gemini embeddings. "
            "Install with: pip install google-genai"
        ) from e


def hash_embed(text: str, dims: int = 1536) -> list[float]:
    """
    Deterministic local embedding fallback.
    Produces stable vectors without external API calls.
    No semantic understanding — same concept with different words gets different vectors.
    """
    if dims <= 0:
        raise ValueError("dims must be > 0")
    if not text:
        return [0.0] * dims

    vec = [0.0] * dims
    words = text.lower().split()
    for idx, token in enumerate(words):
        digest = hashlib.blake2b(f"{idx}:{token}".encode("utf-8"), digest_size=32).digest()
        # Use 8 chunks of 4 bytes each to spread influence across dimensions.
        for j in range(8):
            num = struct.unpack(">I", digest[j * 4 : (j + 1) * 4])[0]
            slot = (num + idx + j) % dims
            sign = 1.0 if (num & 1) == 0 else -1.0
            vec[slot] += sign * ((num % 1000) / 1000.0)
    return normalize(vec)


def normalize(vec: list[float]) -> list[float]:
    mag = math.sqrt(sum(v * v for v in vec))
    if mag == 0:
        return vec
    return [v / mag for v in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("vectors must have same dimension")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    # vectors are normalized, but clamp for numeric safety.
    return max(-1.0, min(1.0, dot))
