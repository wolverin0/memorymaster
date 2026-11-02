from __future__ import annotations

import hashlib
import importlib.util
import logging
import math
import operator
import os
import struct
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Gemini batch embedding rejects more than 100 requests per call (HTTP 400).
GEMINI_MAX_BATCH = 100


class EncoderUnavailable(RuntimeError):
    """No embedding backend can answer: the model child died (T-2004), the provider is
    unavailable, or a remote embedding call failed. Callers rank lexically instead."""


@dataclass(slots=True)
class EmbeddingProvider:
    model: str = "hash-v1"
    dims: int = 1536
    _transformer: Any = field(default=None, repr=False)

    @property
    def is_semantic(self) -> bool:
        """True if this provider produces real semantic embeddings (not hash-v1)."""
        return not self.model.startswith("hash")

    def embed(self, text: str) -> list[float]:
        if self.model.startswith("hash"):
            return hash_embed(text, dims=self.dims)
        if self.model.startswith("gemini:"):
            return self._gemini_embed(text)
        return self._semantic_embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a bounded candidate batch with one provider operation."""
        if not texts:
            return []
        if self.model.startswith("hash"):
            return [hash_embed(text, dims=self.dims) for text in texts]
        if self.model.startswith("gemini:"):
            return self._gemini_embed_batch(texts)
        if self._transformer is None:
            self._transformer = _load_transformer(self.model)
        embeddings = self._transformer.encode(texts, normalize_embeddings=True)
        self.dims = len(embeddings[0])
        return [embedding.tolist() for embedding in embeddings]

    def _semantic_embed(self, text: str) -> list[float]:
        if self._transformer is None:
            self._transformer = _load_transformer(self.model)
        embedding = self._transformer.encode(text, normalize_embeddings=True)
        self.dims = len(embedding)
        return embedding.tolist()

    def _gemini_call(self, contents: Any, units: int) -> Any:
        """One Gemini embed_content request; any failure is EncoderUnavailable, never hash vectors."""
        usage = None
        try:
            if self._transformer is None:
                self._transformer = _load_gemini_client()
            from memorymaster.core.usage_ledger import reserve_configured

            usage = reserve_configured(
                operation="embedding", provider="gemini", actor="account", units=units
            )
            return self._transformer.models.embed_content(
                model=self.model.split(":", 1)[1],
                contents=contents,
            )
        except Exception as exc:  # incl. UsageQuotaExceeded, 4xx/5xx, timeout, missing client
            raise EncoderUnavailable(f"Gemini embedding failed ({type(exc).__name__}: {str(exc)[:200]})") from exc
        finally:
            if usage is not None:
                usage[0].finish(usage[1], outcome="attempted")

    def _gemini_embed(self, text: str) -> list[float]:
        vec = self._gemini_call(text, units=1).embeddings[0].values
        self.dims = len(vec)
        return normalize(list(vec))

    def _gemini_embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), GEMINI_MAX_BATCH):
            chunk = texts[start : start + GEMINI_MAX_BATCH]
            result = self._gemini_call(chunk, units=len(chunk))
            part = [normalize(list(item.values)) for item in result.embeddings]
            if len(part) != len(chunk):
                raise ValueError("embedding provider returned an incomplete batch")
            vectors.extend(part)
        self.dims = len(vectors[0])
        return vectors


@dataclass(slots=True)
class UnavailableProvider(EmbeddingProvider):
    """Stand-in when no embedding backend was selected or could load.

    Every embed call raises :class:`EncoderUnavailable`, so ``query_rows`` ranks lexically and
    steward jobs skip, instead of silently producing hash vectors or sending text anywhere.
    """

    model: str = "unavailable"
    reason: str = "no embedding backend is available"

    @property
    def is_semantic(self) -> bool:
        return False

    def embed(self, text: str) -> list[float]:
        raise EncoderUnavailable(self.reason)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise EncoderUnavailable(self.reason)


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
    model: str = "gemini-embedding-001",
    api_key: str | None = None,
) -> EmbeddingProvider:
    """Create a provider using the Gemini embedding API.

    Requires: pip install google-genai
    Set GEMINI_API_KEY or GOOGLE_API_KEY env var, or pass api_key directly.

    Model default is ``gemini-embedding-001`` (Google's current general-purpose
    embedding model, 768 default dims). The older ``text-embedding-004`` was
    removed from the v1beta surface in 2026-Q1 and returns 404.
    """
    client = _load_gemini_client(api_key=api_key)
    provider = EmbeddingProvider(model=f"gemini:{model}", dims=768)
    provider._transformer = client
    return provider


ENV_EMBEDDING_PROVIDER = "MEMORYMASTER_EMBEDDING_PROVIDER"
_PROVIDER_ALIASES = {
    "": "auto",
    "auto": "auto",
    "sentence-transformers": "sentence-transformers",
    "sentence_transformers": "sentence-transformers",
    "st": "sentence-transformers",
    "gemini": "gemini",
    "hash": "hash",
    "hash-v1": "hash",
}


def configured_embedding_provider() -> str:
    """Operator-selected provider: auto, sentence-transformers, gemini or hash.

    Unknown values read as ``auto``.
    """
    raw = os.environ.get(ENV_EMBEDDING_PROVIDER, "auto").strip().lower()
    return _PROVIDER_ALIASES.get(raw, "auto")


def create_best_provider() -> EmbeddingProvider:
    """Pick the embedding provider the operator asked for; never egress or fake vectors implicitly.

    * ``auto`` (default): local sentence-transformers if it loads, else an
      :class:`UnavailableProvider`. It never selects Gemini (that sends claim text and queries
      to Google) and never selects hash.
    * ``sentence-transformers``: the same, explicitly.
    * ``gemini``: the Gemini embedding API; the only way to send text to Google. Needs a key
      and google-genai, else unavailable.
    * ``hash``: deterministic hash-v1 vectors, the only way to get them (tests).

    Never raises; an unusable choice yields an :class:`UnavailableProvider`
    (embed -> EncoderUnavailable).
    """
    choice = configured_embedding_provider()
    if choice == "hash":
        return EmbeddingProvider(model="hash-v1", dims=1536)

    if choice == "gemini":
        gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not gemini_key:
            return UnavailableProvider(reason="Gemini embeddings selected but no API key is set")
        try:
            provider = create_gemini_provider(api_key=gemini_key)
        except Exception as exc:  # ImportError: google-genai missing; others: client setup
            logger.warning("Gemini embedding API unavailable: %s", type(exc).__name__)
            return UnavailableProvider(reason=f"Gemini embeddings unavailable ({type(exc).__name__})")
        logger.info("Using Gemini embedding API for embeddings (explicitly configured)")
        return provider

    try:
        provider = create_semantic_provider("all-MiniLM-L6-v2")
    except ImportError as exc:
        logger.info("sentence-transformers not installed; embeddings unavailable (lexical ranking)")
        return UnavailableProvider(reason=f"sentence-transformers is not installed: {exc}")
    except Exception as exc:
        logger.warning("sentence-transformers failed to load: %s", exc)
        return UnavailableProvider(reason=f"sentence-transformers failed to load ({type(exc).__name__})")
    logger.info("Using sentence-transformers (all-MiniLM-L6-v2) for embeddings")
    return provider


# One loaded model per process: the shared HTTP server builds a service per
# request, and reloading all-MiniLM-L6-v2 each time cost ~2.7 s per hybrid call (T-0726).
_TRANSFORMERS: dict[str, Any] = {}
_TRANSFORMERS_LOCK = threading.Lock()


_ISOLATE = False


def isolate_embedding_model() -> None:
    """Run the local embedding model in a child process from now on (T-2004).

    Called by the long-lived MCP servers so they never map torch (~2.65 GB); short CLI
    and hook processes keep loading it in-process, which is faster for one query.
    """
    global _ISOLATE
    _ISOLATE = True


def _load_transformer(model: str) -> Any:
    if _ISOLATE:
        if importlib.util.find_spec("sentence_transformers") is None:
            raise ImportError("sentence-transformers is not installed; the embedding child could not load it")
        with _TRANSFORMERS_LOCK:
            transformer = _TRANSFORMERS.get(model)
            if transformer is None:
                from memorymaster.recall.dense_worker import WorkerTransformer  # numpy: only when isolated

                transformer = _TRANSFORMERS[model] = WorkerTransformer(model)
        return transformer
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        raise ImportError(
            "sentence-transformers is required for semantic embeddings. "
            "Install with: pip install sentence-transformers\n"
            "Without it, hybrid recall ranks lexically (or set MEMORYMASTER_EMBEDDING_PROVIDER=hash for tests)."
        ) from e
    with _TRANSFORMERS_LOCK:
        transformer = _TRANSFORMERS.get(model)
        if transformer is None:
            transformer = _TRANSFORMERS[model] = SentenceTransformer(model)
    return transformer


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
    dot = sum(map(operator.mul, a, b))
    # vectors are normalized, but clamp for numeric safety.
    return max(-1.0, min(1.0, dot))


def embedding_content_hash(text: str) -> str:
    """Stable fingerprint of the exact authoritative text embedded."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
