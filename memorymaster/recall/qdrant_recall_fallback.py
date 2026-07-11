"""Qdrant vector-search fallback for :mod:`memorymaster.recall.context_hook`.

Compatibility helpers are retained for a governed R2.1 reintegration, but
R1.3 quarantines retrieval unconditionally. ``is_fallback_enabled()`` returns
``False`` and ``search()`` raises before loading a model or client.

Historically activated only when:

1.  ``MEMORYMASTER_RECALL_VECTOR_FALLBACK`` env var is truthy (``1``/``true``/...).
2.  ``MEMORYMASTER_QDRANT_URL`` is set.
3.  The primary FTS5 + entity-fanout stages returned fewer candidates than
    the threshold (default: 3).

The historical gates are no longer activation controls during quarantine.
Recall always uses authoritative primary-store rows.

Design notes
------------

* Keeps all imports lazy so the legacy install path (no extras) remains
  crash-free. The ``sentence-transformers`` and ``qdrant-client`` packages
  live in the optional ``[vector]`` extra.
* Uses the same deterministic UUID-v5 point id as
  :mod:`scripts.index_claims_to_qdrant` so searches stay in sync with the
  index script.
* Retains lazy model/client helpers for compatibility, but public read search
  never calls them during quarantine.
* Read search fails closed with ``PermissionError`` during quarantine; sync
  and deterministic point-id helpers remain available.
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
    """Remain fail-closed until R2.1 provides authoritative policy filtering."""
    return False


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
    """Reject direct vector reads until governed candidate rehydration exists."""
    del query_text, collection
    raise PermissionError(
        "Qdrant recall fallback is quarantined pending authoritative policy rehydration."
    )
