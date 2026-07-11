"""Qdrant vector store backend for MemoryMaster.

Uses Qdrant at a network-accessible endpoint as a maintenance index alongside
the primary SQLite/Postgres store. Embeddings come from Ollama
(qwen3-embedding:8b, 4096-dim) via HTTP. Direct reads are quarantined until a
governed planner can rehydrate candidate IDs from the authoritative store.

Environment variables / constructor params:
    QDRANT_URL          – default http://localhost:6333
    OLLAMA_URL          – default http://localhost:11434
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

from memorymaster.core.models import Claim

logger = logging.getLogger(__name__)

DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
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

    def count_points(self) -> int | None:
        """Exact point count in the collection, or None if Qdrant is unreachable.

        Used by jobs/qdrant_reconcile.py as the Qdrant side of the drift
        metric (P1 spec §2.7).
        """
        try:
            resp = self._client.post(
                f"{self.qdrant_url}/collections/{self.collection}/points/count",
                json={"exact": True},
            )
            resp.raise_for_status()
            return int((resp.json().get("result") or {}).get("count", 0))
        except Exception as exc:
            logger.warning("Qdrant count failed: %s", exc)
            return None

    def list_point_claim_ids(self, *, batch_size: int = 1000) -> list[int] | None:
        """Scroll every point and return its payload claim_id; None on failure.

        Lets the reconciliation job delete points whose claim is archived or
        missing in the primary store — the half of convergence sync_all
        (upsert-only) cannot do.
        """
        ids: list[int] = []
        offset: Any = None
        try:
            while True:
                body: dict[str, Any] = {
                    "limit": batch_size,
                    "with_payload": ["claim_id"],
                    "with_vector": False,
                }
                if offset is not None:
                    body["offset"] = offset
                resp = self._client.post(
                    f"{self.qdrant_url}/collections/{self.collection}/points/scroll",
                    json=body,
                )
                resp.raise_for_status()
                result = resp.json().get("result") or {}
                for point in result.get("points", []):
                    claim_id = (point.get("payload") or {}).get("claim_id")
                    if claim_id is not None:
                        ids.append(int(claim_id))
                offset = result.get("next_page_offset")
                if offset is None:
                    return ids
        except Exception as exc:
            logger.warning("Qdrant scroll failed: %s", exc)
            return None

    def search(
        self,
        query_text: str,
        limit: int = 5,
        *,
        min_confidence: float = 0.0,
        states: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Reject raw payload reads until the governed planner rehydrates IDs."""
        del query_text, limit, min_confidence, states
        raise PermissionError(
            "Qdrant retrieval is quarantined pending authoritative policy rehydration."
        )

    def _batch_upsert(self, points: list[dict[str, Any]]) -> bool:
        """Upsert a batch of points to Qdrant in a single request."""
        if not points:
            return True
        body = {"points": points}
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
                    logger.info("Qdrant batch upsert attempt %d failed (%s), retrying in %.1fs", attempt + 1, exc, delay)
                    time.sleep(delay)
                else:
                    logger.warning("Qdrant batch upsert failed after %d attempts: %s", 1 + MAX_RETRIES, exc)
        return False

    def sync_all(self, store, *, batch_size: int = 50) -> dict[str, int]:
        """Bulk-push all active claims from the store to Qdrant.

        Uses batch upsert for much better throughput than one-by-one.

        Parameters
        ----------
        store : SQLiteStore | PostgresStore
            The primary data store.
        batch_size : int
            Points per Qdrant upsert request (default 50).

        Returns
        -------
        dict with keys: total, synced, skipped, errors
        """
        self.ensure_collection()
        stats = {"total": 0, "synced": 0, "skipped": 0, "errors": 0}

        for status in ("confirmed", "stale", "candidate", "conflicted"):
            claims = store.find_by_status(status, limit=10_000, include_citations=False)
            stats["total"] += len(claims)
            if claims:
                logger.info("Syncing %d %s claims to Qdrant...", len(claims), status)

            batch: list[dict[str, Any]] = []
            for _idx, claim in enumerate(claims):
                vec = self._embed(self._claim_text(claim))
                if vec is None:
                    stats["errors"] += 1
                    continue
                batch.append({
                    "id": self._point_id(claim.id),
                    "vector": vec,
                    "payload": self._claim_payload(claim),
                })
                if len(batch) >= batch_size:
                    if self._batch_upsert(batch):
                        stats["synced"] += len(batch)
                    else:
                        stats["errors"] += len(batch)
                    batch = []

            # Flush remaining
            if batch:
                if self._batch_upsert(batch):
                    stats["synced"] += len(batch)
                else:
                    stats["errors"] += len(batch)

        logger.info(
            "Qdrant sync_all complete: %d total, %d synced, %d errors",
            stats["total"], stats["synced"], stats["errors"],
        )
        return stats

    def close(self) -> None:
        self._client.close()
