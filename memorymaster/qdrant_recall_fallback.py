"""Qdrant vector-search fallback for :mod:`memorymaster.context_hook`.

Activated only when:

1.  ``MEMORYMASTER_RECALL_VECTOR_FALLBACK`` env var is truthy (``1``/``true``/...).
2.  ``MEMORYMASTER_QDRANT_URL`` is set.
3.  The primary FTS5 + entity-fanout stages returned fewer candidates than
    the threshold (default: 3).

When any of those conditions is false, or when Qdrant / sentence-transformers
is unreachable, the caller silently skips the fallback — default recall
behaviour is unchanged.

Design notes
------------

* Keeps all imports lazy so the legacy install path (no extras) remains
  crash-free. The ``sentence-transformers`` and ``qdrant-client`` packages
  live in the optional ``[vector]`` extra.
* Uses the same deterministic UUID-v5 point id as
  :mod:`scripts.index_claims_to_qdrant` so searches stay in sync with the
  index script.
* Caches the embedder and Qdrant client as module-level singletons across
  calls to amortise model-load cost (~2-3s cold, <5ms warm).
* Never throws across module boundary: every public helper returns safe
  defaults on failure and logs at WARNING.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = "memorymaster-claims"
DEFAULT_EMBED_MODEL = "all-MiniLM-L6-v2"
DEFAULT_SCORE_THRESHOLD = 0.30
DEFAULT_LIMIT = 10
# Trigger the fallback only when <3 primary candidates surfaced.
DEFAULT_MIN_CANDIDATE_THRESHOLD = 3

# Must match scripts/index_claims_to_qdrant.py so SEARCH and INDEX agree
# on point ids — enables idempotent upserts.
_POINT_NAMESPACE = uuid.UUID("6e9a0f8a-0000-5000-8000-000000000001")


def point_id_for_claim(claim_id: int) -> str:
    """Deterministic point id shared with the indexer."""
    return str(uuid.uuid5(_POINT_NAMESPACE, f"mm-claim-{claim_id}"))


@dataclass(frozen=True)
class VectorHit:
    """One Qdrant hit translated into the retrieval pipeline's vocabulary."""

    claim_id: int
    score: float
    scope: str
    subject: str
    status: str
    confidence: float


def _truthy(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def is_fallback_enabled() -> bool:
    """Gate: env opt-in + Qdrant URL configured."""
    if not _truthy(os.environ.get("MEMORYMASTER_RECALL_VECTOR_FALLBACK")):
        return False
    if not os.environ.get("MEMORYMASTER_QDRANT_URL", "").strip():
        return False
    return True


def fallback_threshold() -> int:
    raw = os.environ.get("MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES")
    if raw is None or not raw.strip():
        return DEFAULT_MIN_CANDIDATE_THRESHOLD
    try:
        val = int(raw)
        return val if val >= 0 else DEFAULT_MIN_CANDIDATE_THRESHOLD
    except ValueError:
        return DEFAULT_MIN_CANDIDATE_THRESHOLD


def score_threshold() -> float:
    raw = os.environ.get("MEMORYMASTER_RECALL_VECTOR_SCORE_THRESHOLD")
    if raw is None or not raw.strip():
        return DEFAULT_SCORE_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_SCORE_THRESHOLD


def search_limit() -> int:
    raw = os.environ.get("MEMORYMASTER_RECALL_VECTOR_LIMIT")
    if raw is None or not raw.strip():
        return DEFAULT_LIMIT
    try:
        val = int(raw)
        return val if val > 0 else DEFAULT_LIMIT
    except ValueError:
        return DEFAULT_LIMIT


# ---------------------------------------------------------------------------
# Client / model singletons — loaded lazily, cached for the process lifetime.
# ---------------------------------------------------------------------------

_embedder_lock = threading.Lock()
_embedder: Any | None = None
_embedder_failed = False

_client_lock = threading.Lock()
_client: Any | None = None
_client_failed = False


def _get_embedder():
    """Load ``sentence-transformers`` model lazily; cache the instance.

    Returns ``None`` and disables itself on any failure (ImportError,
    missing weights, bad model id, etc).
    """
    global _embedder, _embedder_failed
    if _embedder is not None:
        return _embedder
    if _embedder_failed:
        return None
    model_name = (
        os.environ.get("MEMORYMASTER_EMBED_MODEL") or DEFAULT_EMBED_MODEL
    )
    with _embedder_lock:
        if _embedder is not None:
            return _embedder
        if _embedder_failed:
            return None
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            logger.warning(
                "vector fallback disabled: sentence-transformers not installed "
                "(install with `pip install memorymaster[vector]`)."
            )
            _embedder_failed = True
            return None
        except Exception as exc:
            logger.warning("vector fallback disabled: import error: %s", exc)
            _embedder_failed = True
            return None
        try:
            _embedder = SentenceTransformer(model_name)
        except Exception as exc:
            logger.warning(
                "vector fallback disabled: failed to load model %r: %s",
                model_name,
                exc,
            )
            _embedder_failed = True
            return None
    return _embedder


def _get_client():
    """Load ``qdrant-client`` lazily; cache the instance.

    Returns ``None`` on any failure and permanently disables the fallback
    for this process so we don't spam retries per-recall.
    """
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed:
        return None
    url = os.environ.get("MEMORYMASTER_QDRANT_URL", "").strip()
    if not url:
        return None
    with _client_lock:
        if _client is not None:
            return _client
        if _client_failed:
            return None
        try:
            from qdrant_client import QdrantClient
        except ImportError:
            logger.warning(
                "vector fallback disabled: qdrant-client not installed "
                "(install with `pip install memorymaster[vector]`)."
            )
            _client_failed = True
            return None
        except Exception as exc:
            logger.warning("vector fallback disabled: import error: %s", exc)
            _client_failed = True
            return None
        try:
            _client = QdrantClient(url=url, timeout=5.0)
        except Exception as exc:
            logger.warning(
                "vector fallback disabled: could not create client for %s: %s",
                url, exc,
            )
            _client_failed = True
            return None
    return _client


def reset_singletons_for_tests() -> None:
    """Test helper — forget cached embedder / client and failure flags."""
    global _embedder, _embedder_failed, _client, _client_failed
    _embedder = None
    _embedder_failed = False
    _client = None
    _client_failed = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def search(query_text: str, *, collection: str | None = None) -> list[VectorHit]:
    """Return up to ``MEMORYMASTER_RECALL_VECTOR_LIMIT`` hits ranked by Qdrant
    cosine similarity, filtered by ``MEMORYMASTER_RECALL_VECTOR_SCORE_THRESHOLD``.

    Never raises: logs a warning and returns an empty list on any failure.
    """
    if not query_text or not query_text.strip():
        return []
    embedder = _get_embedder()
    if embedder is None:
        return []
    client = _get_client()
    if client is None:
        return []
    coll = (
        collection
        or os.environ.get("MEMORYMASTER_QDRANT_COLLECTION")
        or DEFAULT_COLLECTION
    )
    try:
        vec = embedder.encode(
            query_text, normalize_embeddings=True, show_progress_bar=False
        )
        vec_list = vec.tolist() if hasattr(vec, "tolist") else list(vec)
    except Exception as exc:
        logger.warning("vector fallback: embed failed: %s", exc)
        return []

    threshold = score_threshold()
    limit = search_limit()
    try:
        # qdrant-client >=1.10 deprecated `search` in favour of `query_points`.
        # Keep a fallback for older clients so installs pinned to 1.7-1.9 still work.
        if hasattr(client, "query_points"):
            resp = client.query_points(
                collection_name=coll,
                query=vec_list,
                limit=limit,
                score_threshold=threshold,
                with_payload=True,
            )
            raw_hits = getattr(resp, "points", None) or resp
        else:
            raw_hits = client.search(  # type: ignore[attr-defined]
                collection_name=coll,
                query_vector=vec_list,
                limit=limit,
                score_threshold=threshold,
                with_payload=True,
            )
    except Exception as exc:
        logger.warning(
            "vector fallback: qdrant search on %r failed: %s", coll, exc,
        )
        return []

    hits: list[VectorHit] = []
    for h in raw_hits:
        payload = getattr(h, "payload", None) or {}
        raw_id = payload.get("id")
        try:
            claim_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        hits.append(
            VectorHit(
                claim_id=claim_id,
                score=float(getattr(h, "score", 0.0) or 0.0),
                scope=str(payload.get("scope") or ""),
                subject=str(payload.get("subject") or ""),
                status=str(payload.get("status") or ""),
                confidence=float(payload.get("confidence") or 0.0),
            )
        )
    return hits
