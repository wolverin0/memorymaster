"""Write-side claim creation and update methods for SQLiteStore.

This is a mixin class for memorymaster.storage.SQLiteStore. All methods
expect to be bound to a SQLiteStore instance and rely on `self.connect()`
and `self.db_path`. Do not instantiate directly.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.embeddings import EmbeddingProvider, cosine_similarity
from memorymaster.models import (
    CLAIM_LINK_TYPES,
    CLAIM_STATUSES,
    STATUS_TRANSITION_EVENT_TYPES,
    Citation,
    CitationInput,
    Claim,
    ClaimLink,
    Event,
    validate_event_payload,
    validate_event_type,
    validate_transition_event_type,
)

logger = logging.getLogger(__name__)

from memorymaster._storage_shared import (
    EVENT_HASH_ALGO,
    HUMAN_ID_PREFIX,
    SQLITE_CONFIRMED_TUPLE_GUARD_TRIGGERS,
    SQLITE_EVENTS_APPEND_ONLY_TRIGGERS,
    ConcurrentModificationError,
    generate_human_id_hash,
    generate_top_level_human_id,
    utc_now,
)


class _WriteClaimsMixin:

    def create_claim(
        self,
        text: str,
        citations: list[CitationInput],
        *,
        idempotency_key: str | None = None,
        claim_type: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        object_value: str | None = None,
        scope: str = "project",
        volatility: str = "medium",
        confidence: float = 0.5,
        tenant_id: str | None = None,
        event_time: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        source_agent: str | None = None,
        visibility: str = "public",
    ) -> Claim:
        if not citations:
            raise ValueError("At least one citation is required.")
        normalized_idempotency_key = (idempotency_key or "").strip() or None
        normalized_tenant_id = (tenant_id or "").strip() or None
        now = utc_now()
        with self.connect() as conn:
            existing = self._check_idempotency(conn, idempotency_key)
            if existing is not None:
                return existing

            try:
                cur = conn.execute(
                    """
                    INSERT INTO claims (
                        text, idempotency_key, normalized_text, claim_type, subject, predicate, object_value,
                        scope, volatility, status, confidence, pinned, supersedes_claim_id,
                        replaced_by_claim_id, created_at, updated_at, last_validated_at, archived_at,
                        tenant_id, event_time, valid_from, valid_until, source_agent, visibility
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'candidate', ?, 0, NULL, NULL, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        text,
                        normalized_idempotency_key,
                        claim_type,
                        subject,
                        predicate,
                        object_value,
                        scope,
                        volatility,
                        confidence,
                        now,
                        now,
                        normalized_tenant_id,
                        event_time or None,
                        valid_from or now,  # Auto-populate: claim is valid from creation time
                        valid_until or None,
                        source_agent or None,
                        visibility or "public",
                    ),
                )
            except sqlite3.IntegrityError:
                if normalized_idempotency_key is None:
                    raise
                conn.rollback()
                existing_row = conn.execute(
                    "SELECT id FROM claims WHERE idempotency_key = ?",
                    (normalized_idempotency_key,),
                ).fetchone()
                if existing_row is None:
                    raise
                existing = self.get_claim(int(existing_row["id"]))
                if existing is None:
                    raise RuntimeError("Idempotency key matched missing claim.") from None
                return existing

            claim_id = int(cur.lastrowid)
            # Assign a human-readable ID.
            try:
                human_id = self._allocate_human_id(conn, subject, text, claim_id)
                conn.execute(
                    "UPDATE claims SET human_id = ? WHERE id = ?",
                    (human_id, claim_id),
                )
            except sqlite3.OperationalError:
                # Column may not exist in legacy schemas; skip gracefully.
                pass
            for cite in citations:
                # Accept both CitationInput objects and plain dicts
                if isinstance(cite, dict):
                    _src = cite.get("source", "")
                    _loc = cite.get("locator")
                    _exc = cite.get("excerpt")
                else:
                    _src = cite.source
                    _loc = cite.locator
                    _exc = cite.excerpt
                conn.execute(
                    """
                    INSERT INTO citations (claim_id, source, locator, excerpt, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (claim_id, _src, _loc, _exc, now),
                )
            ingest_payload = validate_event_payload(
                "ingest",
                {"citation_count": len(citations)},
                details="claim_ingested",
            )
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="ingest",
                from_status=None,
                to_status="candidate",
                details="claim_ingested",
                payload_json=json.dumps(ingest_payload),
                created_at=now,
            )
            conn.commit()
        claim = self.get_claim(claim_id)
        if claim is None:
            raise RuntimeError("Failed to load claim after insert.")
        return claim


    def set_normalized_text(self, claim_id: int, normalized_text: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE claims SET normalized_text = ?, updated_at = ? WHERE id = ?",
                (normalized_text, now, claim_id),
            )
            conn.commit()


    def set_normalized_texts_batch(self, updates: dict[int, str]) -> None:
        """Batch update normalized_text for multiple claims in a single transaction.

        Args:
            updates: dict mapping claim_id -> normalized_text
        """
        if not updates:
            return
        now = utc_now()
        with self.connect() as conn:
            for claim_id, normalized_text in updates.items():
                conn.execute(
                    "UPDATE claims SET normalized_text = ?, updated_at = ? WHERE id = ?",
                    (normalized_text, now, claim_id),
                )
            conn.commit()


    def redact_claim_payload(
        self,
        claim_id: int,
        *,
        mode: str = "redact",
        redact_claim: bool = True,
        redact_citations: bool = True,
        reason: str | None = None,
        actor: str = "system",
    ) -> dict[str, object]:
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"redact", "erase"}:
            raise ValueError("mode must be one of: redact, erase.")
        if not redact_claim and not redact_citations:
            raise ValueError("At least one of redact_claim or redact_citations must be true.")

        now = utc_now()
        details = "claim_payload_redacted" if normalized_mode == "redact" else "claim_payload_erased"
        claim_text = "[REDACTED_CLAIM_TEXT]" if normalized_mode == "redact" else "[ERASED_CLAIM_TEXT]"
        subject_value = "[REDACTED]" if normalized_mode == "redact" else None
        predicate_value = "[REDACTED]" if normalized_mode == "redact" else None
        object_value = "[REDACTED]" if normalized_mode == "redact" else None
        citation_source = "[REDACTED_SOURCE]" if normalized_mode == "redact" else "[ERASED_SOURCE]"
        citation_locator = "[REDACTED_LOCATOR]" if normalized_mode == "redact" else None
        citation_excerpt = "[REDACTED_EXCERPT]" if normalized_mode == "redact" else None

        with self.connect() as conn:
            status_row = conn.execute("SELECT status FROM claims WHERE id = ?", (claim_id,)).fetchone()
            if status_row is None:
                raise ValueError(f"Claim {claim_id} does not exist.")
            current_status = str(status_row["status"]) if status_row["status"] is not None else None

            claim_rows = 0
            citation_rows = 0

            if redact_claim:
                cur = conn.execute(
                    """
                    UPDATE claims
                    SET text = ?,
                        normalized_text = NULL,
                        subject = ?,
                        predicate = ?,
                        object_value = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (claim_text, subject_value, predicate_value, object_value, now, claim_id),
                )
                claim_rows = int(cur.rowcount)

            if redact_citations:
                cur = conn.execute(
                    """
                    UPDATE citations
                    SET source = ?, locator = ?, excerpt = ?
                    WHERE claim_id = ?
                    """,
                    (citation_source, citation_locator, citation_excerpt, claim_id),
                )
                citation_rows = int(cur.rowcount)
                if not redact_claim:
                    conn.execute(
                        "UPDATE claims SET updated_at = ? WHERE id = ?",
                        (now, claim_id),
                    )

            payload: dict[str, object] = {
                "source": str(actor or "system"),
                "mode": normalized_mode,
                "redact_claim": bool(redact_claim),
                "redact_citations": bool(redact_citations),
                "claim_rows": claim_rows,
                "citation_rows": citation_rows,
            }
            if reason and reason.strip():
                payload["reason"] = reason.strip()
            validated_payload = validate_event_payload("audit", payload, details=details)
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="audit",
                from_status=current_status,
                to_status=current_status,
                details=details,
                payload_json=json.dumps(validated_payload) if validated_payload is not None else None,
                created_at=now,
            )
            conn.commit()

        return {
            "claim_id": claim_id,
            "mode": normalized_mode,
            "redact_claim": bool(redact_claim),
            "redact_citations": bool(redact_citations),
            "claim_rows": claim_rows,
            "citation_rows": citation_rows,
            "event_details": details,
        }


    def update_claim_structure(
        self,
        claim_id: int,
        *,
        claim_type: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        object_value: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE claims
                SET claim_type = COALESCE(claim_type, ?),
                    subject = COALESCE(subject, ?),
                    predicate = COALESCE(predicate, ?),
                    object_value = COALESCE(object_value, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (claim_type, subject, predicate, object_value, now, claim_id),
            )
            conn.commit()


    def set_confidence(self, claim_id: int, confidence: float, details: str | None = None) -> None:
        bounded = max(0.0, min(1.0, confidence))
        now = utc_now()
        with self.connect() as conn:
            # Read status BEFORE update to avoid race condition in event audit trail
            current_status = None
            if details:
                status_row = conn.execute("SELECT status FROM claims WHERE id = ?", (claim_id,)).fetchone()
                current_status = str(status_row["status"]) if status_row else None
            conn.execute(
                "UPDATE claims SET confidence = ?, updated_at = ? WHERE id = ?",
                (bounded, now, claim_id),
            )
            if details:
                self._insert_event_row(
                    conn,
                    claim_id=claim_id,
                    event_type="confidence",
                    from_status=current_status,
                    to_status=current_status,
                    details=details,
                    payload_json=None,
                    created_at=now,
                )
            conn.commit()


    def set_pinned(self, claim_id: int, pinned: bool, reason: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE claims SET pinned = ?, updated_at = ? WHERE id = ?",
                (1 if pinned else 0, now, claim_id),
            )
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="pin" if pinned else "unpin",
                from_status=None,
                to_status=None,
                details=reason,
                payload_json=None,
                created_at=now,
            )
            conn.commit()

