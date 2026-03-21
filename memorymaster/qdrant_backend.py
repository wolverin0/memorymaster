"""Qdrant vector store backend for MemoryMaster.

Uses Qdrant at a network-accessible endpoint as a search index alongside
the primary SQLite/Postgres store.  Embeddings come from Ollama
(qwen3-embedding:8b, 4096-dim) via HTTP.

Environment variables / constructor params:
    QDRANT_URL          – default http://192.168.100.186:6333
    OLLAMA_URL          – default http://192.168.100.155:11434
    QDRANT_COLLECTION   – default "agent-memories"
    OLLAMA_EMBED_MODEL  – default "qwen3-embedding:8b"
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

import httpx

from memorymaster.models import Claim

logger = logging.getLogger(__name__)

DEFAULT_QDRANT_URL = "http://192.168.100.186:6333"
DEFAULT_OLLAMA_URL = "http://192.168.100.155:11434"
DEFAULT_COLLECTION = "agent-memories"
DEFAULT_EMBED_MODEL = "qwen3-embedding:8b"
EMBEDDING_DIMS = 4096
OLLAMA_TIMEOUT = 120.0
MAX_RETRIES = 2
RETRY_BASE_DELAY = 0.5


class QdrantBackend:
    """Thin wrapper around the Qdrant REST API for claim indexing."""

    def __init__(
        self,
        qdrant_url: str | None = None,
        ollama_url: str | None = None,
        collection: str | None = None,
        embed_model: str | None = None,
    ) -> None:
        self.qdrant_url = (qdrant_url or os.environ.get("QDRANT_URL") or DEFAULT_QDRANT_URL).rstrip("/")
        self.ollama_url = (ollama_url or os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
        self.collection = collection or os.environ.get("QDRANT_COLLECTION") or DEFAULT_COLLECTION
        self.embed_model = embed_model or os.environ.get("OLLAMA_EMBED_MODEL") or DEFAULT_EMBED_MODEL
        self._client = httpx.Client(timeout=30.0)

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    def ensure_collection(self) -> None:
        """Create the Qdrant collection if it does not exist."""
        url = f"{self.qdrant_url}/collections/{self.collection}"
        resp = self._client.get(url)
        if resp.status_code == 200:
            logger.debug("Qdrant collection '%s' already exists", self.collection)
            return
        body = {
            "vectors": {
                "size": EMBEDDING_DIMS,
                "distance": "Cosine",
            }
        }
        resp = self._client.put(url, json=body)
        resp.raise_for_status()
        logger.info("Created Qdrant collection '%s' (%d dims, Cosine)", self.collection, EMBEDDING_DIMS)

    # ------------------------------------------------------------------
    # Embedding via Ollama
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> list[float] | None:
        """Get a 4096-dim embedding from Ollama with retry on transient failures."""
        for attempt in range(1 + MAX_RETRIES):
            try:
                resp = self._client.post(
                    f"{self.ollama_url}/api/embed",
                    json={"model": self.embed_model, "input": [text]},
                    timeout=OLLAMA_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                vectors = data.get("embeddings") or []
                if vectors and len(vectors[0]) == EMBEDDING_DIMS:
                    return vectors[0]
                logger.warning(
                    "Ollama returned unexpected dims: got %d, expected %d",
                    len(vectors[0]) if vectors else 0,
                    EMBEDDING_DIMS,
                )
                return None  # dim mismatch is not retryable
            except Exception as exc:
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.info("Ollama embed attempt %d failed (%s), retrying in %.1fs", attempt + 1, exc, delay)
                    time.sleep(delay)
                else:
                    logger.warning("Ollama embed failed after %d attempts: %s", 1 + MAX_RETRIES, exc)
        return None

    # ------------------------------------------------------------------
    # Claim → Qdrant payload helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _claim_text(claim: Claim) -> str:
        """Build the text string used for embedding a claim."""
        parts = []
        if claim.subject:
            parts.append(claim.subject)
        if claim.predicate:
            parts.append(claim.predicate)
        if claim.object_value:
            parts.append(claim.object_value)
        parts.append(claim.text)
        return " ".join(parts)

    @staticmethod
    def _claim_payload(claim: Claim, source: str = "memorymaster") -> dict[str, Any]:
        return {
            "claim_id": claim.id,
            "subject": claim.subject or "",
            "predicate": claim.predicate or "",
            "object": claim.object_value or "",
            "claim_text": claim.text,
            "state": claim.status,
            "confidence": claim.confidence,
            "source": source,
            "created_at": claim.created_at,
            "workspace": "main",
        }

    @staticmethod
    def _point_id(claim_id: int) -> str:
        """Deterministic UUID-v5 from claim id so upserts are idempotent."""
        return str(uuid.uuid5(uuid.NAMESPACE_OID, f"mm-claim-{claim_id}"))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def upsert_claim(self, claim: Claim, source: str = "memorymaster") -> bool:
        """Embed and upsert a single claim.  Returns True on success."""
        vec = self._embed(self._claim_text(claim))
        if vec is None:
            return False
        point_id = self._point_id(claim.id)
        body = {
            "points": [
                {
                    "id": point_id,
                    "vector": vec,
                    "payload": self._claim_payload(claim, source=source),
                }
            ]
        }
        for attempt in range(1 + MAX_RETRIES):
            try:
                resp = self._client.put(
                    f"{self.qdrant_url}/collections/{self.collection}/points",
                    json=body,
                )
                resp.raise_for_status()
                return True
            except Exception as exc:
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.info("Qdrant upsert attempt %d for claim %d failed (%s), retrying in %.1fs", attempt + 1, claim.id, exc, delay)
                    time.sleep(delay)
                else:
                    logger.warning("Qdrant upsert failed for claim %d after %d attempts: %s", claim.id, 1 + MAX_RETRIES, exc)
        return False

    def delete_claim(self, claim_id: int) -> bool:
        """Delete a claim's point from Qdrant.  Returns True on success."""
        point_id = self._point_id(claim_id)
        body = {"points": [point_id]}
        try:
            resp = self._client.post(
                f"{self.qdrant_url}/collections/{self.collection}/points/delete",
                json=body,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Qdrant delete failed for claim %d: %s", claim_id, exc)
            return False

    def search(
        self,
        query_text: str,
        limit: int = 5,
        *,
        min_confidence: float = 0.0,
        states: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic search.  Returns list of {claim_id, score, payload}."""
        vec = self._embed(query_text)
        if vec is None:
            return []

        filters: dict[str, Any] = {"must": []}
        if states:
            filters["must"].append({
                "key": "state",
                "match": {"any": states},
            })
        if min_confidence > 0:
            filters["must"].append({
                "key": "confidence",
                "range": {"gte": min_confidence},
            })

        body: dict[str, Any] = {
            "vector": vec,
            "limit": limit,
            "with_payload": True,
        }
        if filters["must"]:
            body["filter"] = filters

        try:
            resp = self._client.post(
                f"{self.qdrant_url}/collections/{self.collection}/points/search",
                json=body,
            )
            resp.raise_for_status()
            results = resp.json().get("result", [])
            return [
                {
                    "claim_id": hit["payload"].get("claim_id"),
                    "score": hit["score"],
                    "payload": hit["payload"],
                }
                for hit in results
            ]
        except Exception as exc:
            logger.warning("Qdrant search failed: %s", exc)
            return []

    def sync_all(self, store, *, batch_size: int = 50) -> dict[str, int]:
        """Bulk-push all confirmed claims from the store to Qdrant.

        Parameters
        ----------
        store : SQLiteStore | PostgresStore
            The primary data store.
        batch_size : int
            How many claims to fetch per page.

        Returns
        -------
        dict with keys: total, synced, skipped, errors
        """
        self.ensure_collection()
        stats = {"total": 0, "synced": 0, "skipped": 0, "errors": 0}

        for status in ("confirmed", "stale", "candidate", "conflicted"):
            claims = store.find_by_status(status, limit=10_000, include_citations=False)
            stats["total"] += len(claims)
            for claim in claims:
                ok = self.upsert_claim(claim)
                if ok:
                    stats["synced"] += 1
                else:
                    stats["errors"] += 1

        logger.info(
            "Qdrant sync_all complete: %d total, %d synced, %d errors",
            stats["total"], stats["synced"], stats["errors"],
        )
        return stats

    def close(self) -> None:
        self._client.close()
