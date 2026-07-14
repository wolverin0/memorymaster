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
import hashlib
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

try:
    import httpx
except ModuleNotFoundError:  # Optional semantic-profile dependency.
    httpx = None  # type: ignore[assignment]

from memorymaster.core.models import Claim
from memorymaster.core.security import scan_persisted_value
from memorymaster.recall.qdrant_transport import QdrantTransportConfig

logger = logging.getLogger(__name__)

DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_COLLECTION = "agent-memories"
DEFAULT_EMBED_MODEL = "qwen3-embedding:8b"
EMBEDDING_DIMS = 4096
OLLAMA_TIMEOUT = 120.0
MAX_RETRIES = 2
RETRY_BASE_DELAY = 0.5
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class QdrantCandidate:
    """Untrusted vector match reduced to the only fields recall may consume."""

    claim_id: int
    content_hash: str
    score: float


def claim_content_hash(claim: Claim) -> str:
    """Stable digest of the authoritative fields represented by one vector."""
    representation = {
        "text": claim.text,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "object_value": claim.object_value,
        "claim_type": claim.claim_type,
        "holder": claim.holder,
        "scope": claim.scope,
        "status": claim.status,
        "tenant_id": claim.tenant_id,
        "visibility": claim.visibility,
        "source_agent": claim.source_agent,
    }
    encoded = json.dumps(
        representation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _create_http_clients(
    transport: QdrantTransportConfig,
) -> tuple[Any, Any]:
    if httpx is None:
        raise RuntimeError("Qdrant support requires `pip install memorymaster[qdrant]`")
    try:
        qdrant_client = httpx.Client(timeout=30.0, **transport.httpx_kwargs())
    except Exception:
        raise RuntimeError("Qdrant client initialization failed") from None
    try:
        ollama_client = httpx.Client(timeout=30.0)
    except Exception:
        try:
            qdrant_client.close()
        except Exception:
            pass
        raise RuntimeError("Ollama client initialization failed") from None
    return qdrant_client, ollama_client


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
        transport = QdrantTransportConfig.from_env()
        transport.validate_url(self.qdrant_url)
        self._qdrant_client, self._ollama_client = _create_http_clients(transport)

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    def ensure_collection(self) -> None:
        """Create the Qdrant collection if it does not exist."""
        try:
            url = f"{self.qdrant_url}/collections/{self.collection}"
            resp = self._qdrant_client.get(url)
            if resp.status_code == 200:
                logger.debug("Qdrant collection '%s' already exists", self.collection)
                return
            body = {
                "vectors": {
                    "size": EMBEDDING_DIMS,
                    "distance": "Cosine",
                }
            }
            resp = self._qdrant_client.put(url, json=body)
            resp.raise_for_status()
        except Exception:
            raise RuntimeError("Qdrant collection request failed") from None
        logger.info("Created Qdrant collection '%s' (%d dims, Cosine)", self.collection, EMBEDDING_DIMS)

    # ------------------------------------------------------------------
    # Embedding via Ollama
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> list[float] | None:
        """Get a 4096-dim embedding from Ollama with retry on transient failures."""
        for attempt in range(1 + MAX_RETRIES):
            try:
                resp = self._ollama_client.post(
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
            except Exception:
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.info(
                        "Ollama embed attempt %d failed; retrying in %.1fs",
                        attempt + 1,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        "Ollama embed failed after %d attempts",
                        1 + MAX_RETRIES,
                    )
        return None

    # ------------------------------------------------------------------
    # Claim → Qdrant payload helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _claim_representation(claim: Claim) -> dict[str, Any]:
        return {
            "text": claim.text,
            "subject": claim.subject,
            "predicate": claim.predicate,
            "object_value": claim.object_value,
            "claim_type": claim.claim_type,
            "scope": claim.scope,
            "citations": [
                {"source": item.source, "locator": item.locator, "excerpt": item.excerpt} for item in claim.citations
            ],
        }

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
        del source
        return {
            "claim_id": claim.id,
            "content_hash": claim_content_hash(claim),
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
        findings = scan_persisted_value(self._claim_representation(claim))
        if findings:
            logger.warning("Qdrant upsert rejected sensitive claim %d (%s)", claim.id, ",".join(findings))
            return False
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
                resp = self._qdrant_client.put(
                    f"{self.qdrant_url}/collections/{self.collection}/points",
                    json=body,
                )
                resp.raise_for_status()
                return True
            except Exception:
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.info(
                        "Qdrant upsert attempt %d for claim %d failed; retrying in %.1fs",
                        attempt + 1,
                        claim.id,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        "Qdrant upsert failed for claim %d after %d attempts",
                        claim.id,
                        1 + MAX_RETRIES,
                    )
        return False

    def delete_claim(self, claim_id: int) -> bool:
        """Delete a claim's point from Qdrant.  Returns True on success."""
        point_id = self._point_id(claim_id)
        body = {"points": [point_id]}
        try:
            resp = self._qdrant_client.post(
                f"{self.qdrant_url}/collections/{self.collection}/points/delete",
                json=body,
            )
            resp.raise_for_status()
            return True
        except Exception:
            logger.warning("Qdrant delete failed for claim %d", claim_id)
            return False

    def count_points(self) -> int | None:
        """Exact point count in the collection, or None if Qdrant is unreachable.

        Used by jobs/qdrant_reconcile.py as the Qdrant side of the drift
        metric (P1 spec §2.7).
        """
        try:
            resp = self._qdrant_client.post(
                f"{self.qdrant_url}/collections/{self.collection}/points/count",
                json={"exact": True},
            )
            resp.raise_for_status()
            return int((resp.json().get("result") or {}).get("count", 0))
        except Exception:
            logger.warning("Qdrant count failed")
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
                resp = self._qdrant_client.post(
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
        except Exception:
            logger.warning("Qdrant scroll failed")
            return None

    def list_point_refs(self, *, batch_size: int = 1000) -> list[QdrantCandidate] | None:
        """Scroll validated claim ID/hash pairs for exact reconciliation."""
        refs: list[QdrantCandidate] = []
        offset: Any = None
        try:
            while True:
                body: dict[str, Any] = {
                    "limit": batch_size,
                    "with_payload": ["claim_id", "content_hash"],
                    "with_vector": False,
                }
                if offset is not None:
                    body["offset"] = offset
                resp = self._qdrant_client.post(
                    f"{self.qdrant_url}/collections/{self.collection}/points/scroll",
                    json=body,
                )
                resp.raise_for_status()
                result = resp.json().get("result") or {}
                for point in result.get("points", []):
                    candidate = self._candidate_from_point(point, default_score=0.0)
                    if candidate is not None:
                        refs.append(candidate)
                offset = result.get("next_page_offset")
                if offset is None:
                    return refs
        except Exception:
            logger.warning("Qdrant reference scroll failed")
            return None

    @staticmethod
    def _candidate_from_point(
        point: object,
        *,
        default_score: float | None = None,
    ) -> QdrantCandidate | None:
        if not isinstance(point, dict):
            return None
        payload = point.get("payload")
        if not isinstance(payload, dict):
            return None
        claim_id = payload.get("claim_id")
        content_hash = payload.get("content_hash")
        score = point.get("score", default_score)
        if isinstance(claim_id, bool) or not isinstance(claim_id, int) or claim_id <= 0:
            return None
        if not isinstance(content_hash, str) or _SHA256_RE.fullmatch(content_hash) is None:
            return None
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return None
        numeric_score = float(score)
        if not math.isfinite(numeric_score):
            return None
        return QdrantCandidate(claim_id, content_hash, numeric_score)

    def search_candidates(self, query_text: str, *, limit: int) -> list[QdrantCandidate]:
        """Return validated ID/hash candidates; never return raw payload data."""
        if limit <= 0 or limit > 1000:
            raise ValueError("Qdrant candidate limit must be between 1 and 1000.")
        vector = self._embed(query_text)
        if vector is None:
            return []
        body = {
            "query": vector,
            "limit": limit,
            "with_payload": ["claim_id", "content_hash"],
            "with_vector": False,
        }
        try:
            resp = self._qdrant_client.post(
                f"{self.qdrant_url}/collections/{self.collection}/points/query",
                json=body,
            )
            resp.raise_for_status()
            result = resp.json().get("result") or {}
            points = result.get("points", []) if isinstance(result, dict) else result
            if not isinstance(points, list):
                return []
            return [
                candidate
                for point in points
                if (candidate := self._candidate_from_point(point)) is not None
            ]
        except Exception:
            logger.warning("Qdrant candidate search failed")
            return []

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
                resp = self._qdrant_client.put(
                    f"{self.qdrant_url}/collections/{self.collection}/points",
                    json=body,
                )
                resp.raise_for_status()
                return True
            except Exception:
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.info(
                        "Qdrant batch upsert attempt %d failed; retrying in %.1fs",
                        attempt + 1,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        "Qdrant batch upsert failed after %d attempts",
                        1 + MAX_RETRIES,
                    )
        return False

    def sync_all(self, store, *, batch_size: int = 50) -> dict[str, int]:
        """Replayably keyset-page every eligible authoritative claim."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.ensure_collection()
        stats = {"total": 0, "synced": 0, "skipped": 0, "errors": 0}
        authority = str(getattr(store, "tenant_id", None) or "local")
        stream_key = f"{self.collection}:{self.embed_model}:{authority}"
        cursor = store.get_qdrant_sync_cursor(stream_key)
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise TypeError("Qdrant sync cursor store returned an invalid cursor")

        while True:
            claims = store.list_qdrant_sync_page(after_id=cursor, limit=batch_size)
            if not isinstance(claims, list):
                raise TypeError("Qdrant sync store returned an invalid page")
            if not claims:
                store.set_qdrant_sync_cursor(stream_key, 0)
                break
            stats["total"] += len(claims)
            points: list[dict[str, Any]] = []
            page_errors = 0
            for claim in claims:
                findings = scan_persisted_value(self._claim_representation(claim))
                if findings:
                    stats["skipped"] += 1
                    logger.warning("Qdrant sync skipped sensitive claim %d (%s)", claim.id, ",".join(findings))
                    continue
                vec = self._embed(self._claim_text(claim))
                if vec is None:
                    stats["errors"] += 1
                    page_errors += 1
                    continue
                points.append({
                    "id": self._point_id(claim.id),
                    "vector": vec,
                    "payload": self._claim_payload(claim),
                })
            if points and self._batch_upsert(points):
                stats["synced"] += len(points)
            elif points:
                stats["errors"] += len(points)
                page_errors += len(points)
            if page_errors:
                break
            cursor = claims[-1].id
            store.set_qdrant_sync_cursor(stream_key, cursor)

        logger.info(
            "Qdrant sync_all complete: %d total, %d synced, %d errors",
            stats["total"], stats["synced"], stats["errors"],
        )
        return stats

    def close(self) -> None:
        self._qdrant_client.close()
        if self._ollama_client is not self._qdrant_client:
            self._ollama_client.close()
