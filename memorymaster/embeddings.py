from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EmbeddingProvider:
    model: str = "hash-v1"
    dims: int = 1536
    _transformer: Any = field(default=None, repr=False)

    def embed(self, text: str) -> list[float]:
        if self.model.startswith("hash"):
            return hash_embed(text, dims=self.dims)
        return self._semantic_embed(text)

    def _semantic_embed(self, text: str) -> list[float]:
        if self._transformer is None:
            self._transformer = _load_transformer(self.model)
        embedding = self._transformer.encode(text, normalize_embeddings=True)
        self.dims = len(embedding)
        return embedding.tolist()


def create_semantic_provider(model: str = "all-MiniLM-L6-v2") -> EmbeddingProvider:
    """Create a provider using sentence-transformers for real semantic embeddings.

    Requires: pip install sentence-transformers
    Models: all-MiniLM-L6-v2 (384-dim, fast), all-mpnet-base-v2 (768-dim, better)
    """
    provider = EmbeddingProvider(model=model, dims=384)
    # Eagerly validate the model loads
    provider._transformer = _load_transformer(model)
    provider.dims = provider._transformer.get_sentence_embedding_dimension()
    return provider


def _load_transformer(model: str) -> Any:
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(model)
    except ImportError:
        raise ImportError(
            "sentence-transformers is required for semantic embeddings. "
            "Install with: pip install sentence-transformers\n"
            "Or use the default hash-v1 model (no dependencies, but not semantic)."
        )


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
    dot = sum(x * y for x, y in zip(a, b))
    # vectors are normalized, but clamp for numeric safety.
    return max(-1.0, min(1.0, dot))
