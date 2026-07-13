"""Atlas Inbox source/evidence/action proposal storage methods.

This is a mixin class for memorymaster.stores.storage.SQLiteStore. Methods expect
``self.connect()``, ``self._insert_event_row()``, and schema initialization from
``_SchemaMixin``.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import TYPE_CHECKING, Any

from memorymaster.stores._storage_shared import utc_now
from memorymaster.core.security import (
    sanitize_persisted_json,
    sanitize_persisted_text,
    scan_persisted_value,
    validate_persisted_metadata,
)
from memorymaster.core.models import (
    ATLAS_SENSITIVITY_LEVELS,
    MEDIA_RETRY_STATUSES,
    ActionProposal,
    EvidenceItem,
    ExternalSource,
    MediaRetryItem,
    SourceItem,
)


def _normalize_sensitivity(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text not in ATLAS_SENSITIVITY_LEVELS:
        raise ValueError(
            f"sensitivity must be one of: {', '.join(ATLAS_SENSITIVITY_LEVELS)} (or None)."
        )
    return text


def _json_or_none(value: dict[str, Any] | str | None) -> str | None:
    if value is None:
        return None
    parsed: object = value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = stripped
    sanitized, _ = sanitize_persisted_json(parsed)
    if isinstance(sanitized, str):
        return sanitized
    return json.dumps(sanitized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _safe_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    sanitized, findings = sanitize_persisted_text(text)
    token_findings = any(
        scan_persisted_value(token)
        for token in re.split(r"[^A-Za-z0-9_+=/-]+|[/\\]", text)
        if token
    )
    return "[REDACTED:encoded_secret]" if token_findings and not findings else sanitized


def _atlas_row_is_safe(row: Any, fields: tuple[str, ...]) -> bool:
    return not any(scan_persisted_value(row[field]) for field in fields if row[field] is not None)


_SOURCE_FIELDS = (
    "source_item_id", "item_type", "chat_id", "sender_id", "sender_name", "occurred_at",
    "text", "payload_json", "content_hash", "sensitivity", "created_at", "updated_at",
)
_EXTERNAL_SOURCE_FIELDS = (
    "source_type", "display_name", "config_json", "created_at", "updated_at",
)
_EVIDENCE_FIELDS = (
    "evidence_type", "text", "media_path", "provider", "payload_json", "sensitivity", "created_at",
)
_ACTION_FIELDS = (
    "proposal_type", "title", "description", "suggested_due_at", "destination", "status",
    "payload_json", "external_ref", "exported_at", "idempotency_key", "created_at", "updated_at",
)
_RETRY_FIELDS = (
    "media_key", "chat_id", "media_type", "media_path", "media_url", "status", "last_error",
    "next_attempt_time", "created_at", "updated_at",
)


def _bounded_confidence(confidence: float | None) -> float | None:
    if confidence is None:
        return None
    return max(0.0, min(1.0, float(confidence)))


class _SourceItemsMixin:
    if TYPE_CHECKING:
        def connect(self) -> sqlite3.Connection: ...

        def _insert_event_row(
            self,
            conn: sqlite3.Connection,
            *,
            claim_id: int | None,
            event_type: str,
            from_status: str | None,
            to_status: str | None,
            details: str | None,
            payload_json: str | None,
            created_at: str,
        ) -> int: ...

    def upsert_external_source(
        self,
        *,
        source_type: str,
        display_name: str,
        config_json: dict[str, Any] | str | None = None,
    ) -> ExternalSource:
        validate_persisted_metadata({"source_type": source_type, "display_name": display_name})
        normalized_source_type = source_type.strip().lower()
        normalized_display_name = display_name.strip()
        if not normalized_source_type:
            raise ValueError("source_type must be non-empty.")
        if not normalized_display_name:
            raise ValueError("display_name must be non-empty.")

        now = utc_now()
        payload = _json_or_none(config_json)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM external_sources WHERE source_type = ? AND display_name = ?",
                (normalized_source_type, normalized_display_name),
            ).fetchone()
            if existing is not None and not _atlas_row_is_safe(existing, _EXTERNAL_SOURCE_FIELDS):
                raise ValueError("Existing external source contains unsafe persisted data.")
            conn.execute(
                """
                INSERT INTO external_sources (source_type, display_name, config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_type, display_name) DO UPDATE SET
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (normalized_source_type, normalized_display_name, payload, now, now),
            )
            row = conn.execute(
                "SELECT * FROM external_sources WHERE source_type = ? AND display_name = ?",
                (normalized_source_type, normalized_display_name),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to upsert external source.")
            if not _atlas_row_is_safe(row, _EXTERNAL_SOURCE_FIELDS):
                raise ValueError("External source contains unsafe persisted data.")
            conn.commit()
        return self._row_to_external_source(row)

    def upsert_source_item(
        self,
        *,
        source_id: int,
        source_item_id: str,
        item_type: str,
        chat_id: str | None = None,
        sender_id: str | None = None,
        sender_name: str | None = None,
        occurred_at: str | None = None,
        text: str | None = None,
        payload_json: dict[str, Any] | str | None = None,
        content_hash: str | None = None,
        sensitivity: str | None = None,
    ) -> SourceItem:
        validate_persisted_metadata({"source_item_id": source_item_id, "item_type": item_type, "content_hash": content_hash, "occurred_at": occurred_at})
        normalized_source_item_id = source_item_id.strip()
        normalized_item_type = item_type.strip().lower()
        if source_id <= 0:
            raise ValueError("source_id must be positive.")
        if not normalized_source_item_id:
            raise ValueError("source_item_id must be non-empty.")
        if not normalized_item_type:
            raise ValueError("item_type must be non-empty.")
        normalized_sensitivity = _normalize_sensitivity(sensitivity)
        chat_id, sender_id, sender_name, text = map(_safe_text, (chat_id, sender_id, sender_name, text))

        now = utc_now()
        payload = _json_or_none(payload_json)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM source_items WHERE source_id = ? AND source_item_id = ?",
                (source_id, normalized_source_item_id),
            ).fetchone()
            if existing is not None and not _atlas_row_is_safe(existing, _SOURCE_FIELDS):
                raise ValueError("Existing source item contains unsafe persisted data.")
            # Preserve existing sensitivity on re-import unless caller passed one
            preserve_sensitivity_clause = (
                "sensitivity = excluded.sensitivity"
                if sensitivity is not None
                else "sensitivity = source_items.sensitivity"
            )
            conn.execute(
                f"""
                INSERT INTO source_items (
                    source_id, source_item_id, item_type, chat_id, sender_id, sender_name,
                    occurred_at, text, payload_json, content_hash, sensitivity, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, source_item_id) DO UPDATE SET
                    item_type = excluded.item_type,
                    chat_id = excluded.chat_id,
                    sender_id = excluded.sender_id,
                    sender_name = excluded.sender_name,
                    occurred_at = excluded.occurred_at,
                    text = excluded.text,
                    payload_json = excluded.payload_json,
                    content_hash = excluded.content_hash,
                    {preserve_sensitivity_clause},
                    updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    normalized_source_item_id,
                    normalized_item_type,
                    chat_id,
                    sender_id,
                    sender_name,
                    occurred_at,
                    text,
                    payload,
                    content_hash,
                    normalized_sensitivity,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM source_items WHERE source_id = ? AND source_item_id = ?",
                (source_id, normalized_source_item_id),
            ).fetchone()
            if existing is None:
                self._insert_event_row(
                    conn,
                    claim_id=None,
                    event_type="source_import",
                    from_status=None,
                    to_status=None,
                    details="source_item_imported",
                    payload_json=json.dumps(
                        {
                            "source_id": source_id,
                            "source_item_id": normalized_source_item_id,
                            "item_type": normalized_item_type,
                        },
                        sort_keys=True,
                    ),
                    created_at=now,
                )
            if row is None:
                raise RuntimeError("Failed to upsert source item.")
            if not _atlas_row_is_safe(row, _SOURCE_FIELDS):
                raise ValueError("Source item contains unsafe persisted data.")
            conn.commit()
        return self._row_to_source_item(row)

    def get_source_item(self, *, source_id: int, source_item_id: str) -> SourceItem | None:
        normalized_source_item_id = source_item_id.strip()
        if source_id <= 0:
            raise ValueError("source_id must be positive.")
        if not normalized_source_item_id:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM source_items WHERE source_id = ? AND source_item_id = ?",
                (source_id, normalized_source_item_id),
            ).fetchone()
        return self._row_to_source_item(row) if row is not None and _atlas_row_is_safe(row, _SOURCE_FIELDS) else None

    def get_source_item_by_id(self, source_item_row_id: int) -> SourceItem | None:
        if source_item_row_id <= 0:
            raise ValueError("source_item_row_id must be positive.")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM source_items WHERE id = ?", (source_item_row_id,)).fetchone()
        return self._row_to_source_item(row) if row is not None and _atlas_row_is_safe(row, _SOURCE_FIELDS) else None

    def add_evidence_item(
        self,
        *,
        source_item_id: int,
        evidence_type: str,
        text: str | None = None,
        media_path: str | None = None,
        provider: str | None = None,
        confidence: float | None = None,
        payload_json: dict[str, Any] | str | None = None,
        sensitivity: str | None = None,
    ) -> EvidenceItem:
        validate_persisted_metadata({"evidence_type": evidence_type})
        normalized_evidence_type = evidence_type.strip().lower()
        if source_item_id <= 0:
            raise ValueError("source_item_id must be positive.")
        if not normalized_evidence_type:
            raise ValueError("evidence_type must be non-empty.")
        normalized_sensitivity = _normalize_sensitivity(sensitivity)
        text, media_path, provider = map(_safe_text, (text, media_path, provider))

        now = utc_now()
        payload = _json_or_none(payload_json)
        bounded = _bounded_confidence(confidence)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO evidence_items (
                    source_item_id, evidence_type, text, media_path, provider,
                    confidence, payload_json, sensitivity, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source_item_id, normalized_evidence_type, text, media_path, provider, bounded, payload, normalized_sensitivity, now),
            )
            evidence_id = int(cur.lastrowid)
            self._insert_event_row(
                conn,
                claim_id=None,
                event_type="media_process",
                from_status=None,
                to_status=None,
                details="evidence_item_added",
                payload_json=json.dumps(
                    {
                        "source_item_id": source_item_id,
                        "evidence_item_id": evidence_id,
                        "evidence_type": normalized_evidence_type,
                    },
                    sort_keys=True,
                ),
                created_at=now,
            )
            row = conn.execute("SELECT * FROM evidence_items WHERE id = ?", (evidence_id,)).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("Failed to add evidence item.")
        return self._row_to_evidence_item(row)

    def list_evidence_items(
        self,
        *,
        source_item_id: int | None = None,
        evidence_type: str | None = None,
        limit: int = 100,
    ) -> list[EvidenceItem]:
        clauses: list[str] = []
        params: list[object] = []
        if source_item_id is not None:
            if source_item_id <= 0:
                raise ValueError("source_item_id must be positive.")
            clauses.append("source_item_id = ?")
            params.append(source_item_id)
        if evidence_type:
            clauses.append("evidence_type = ?")
            params.append(evidence_type.strip().lower())
        if limit <= 0:
            return []
        page_size = min(max(limit, 25), 250)
        results: list[EvidenceItem] = []
        cursor: tuple[object, int] | None = None
        with self.connect() as conn:
            while len(results) < limit:
                page_clauses = list(clauses)
                page_params = list(params)
                if cursor is not None:
                    page_clauses.append("(created_at > ? OR (created_at = ? AND id > ?))")
                    page_params.extend((cursor[0], cursor[0], cursor[1]))
                where_sql = f"WHERE {' AND '.join(page_clauses)}" if page_clauses else ""
                rows = conn.execute(
                    f"SELECT * FROM evidence_items {where_sql} ORDER BY created_at ASC, id ASC LIMIT ?",
                    [*page_params, page_size],
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    if _atlas_row_is_safe(row, _EVIDENCE_FIELDS):
                        results.append(self._row_to_evidence_item(row))
                        if len(results) == limit:
                            break
                cursor = (rows[-1]["created_at"], int(rows[-1]["id"]))
        return results

    def create_action_proposal(
        self,
        *,
        proposal_type: str,
        title: str,
        description: str | None = None,
        source_item_id: int | None = None,
        evidence_item_id: int | None = None,
        claim_id: int | None = None,
        suggested_due_at: str | None = None,
        destination: str = "manual",
        confidence: float = 0.5,
        payload_json: dict[str, Any] | str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionProposal:
        validate_persisted_metadata({"proposal_type": proposal_type, "destination": destination, "idempotency_key": idempotency_key, "suggested_due_at": suggested_due_at})
        normalized_type = proposal_type.strip().lower()
        normalized_title = title.strip()
        normalized_destination = destination.strip() or "manual"
        normalized_idempotency_key = (idempotency_key or "").strip() or None
        normalized_title = _safe_text(normalized_title) or "[REDACTED]"
        description = _safe_text(description)
        if not normalized_type:
            raise ValueError("proposal_type must be non-empty.")
        if not normalized_title:
            raise ValueError("title must be non-empty.")

        if normalized_idempotency_key:
            existing = self.get_action_proposal_by_idempotency_key(normalized_idempotency_key)
            if existing is not None:
                return existing

        now = utc_now()
        payload = _json_or_none(payload_json)
        bounded = _bounded_confidence(confidence)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO action_proposals (
                    proposal_type, title, description, source_item_id, evidence_item_id,
                    claim_id, suggested_due_at, destination, status, confidence, payload_json,
                    exported_at, external_ref, idempotency_key, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    normalized_type,
                    normalized_title,
                    description,
                    source_item_id,
                    evidence_item_id,
                    claim_id,
                    suggested_due_at,
                    normalized_destination,
                    bounded if bounded is not None else 0.5,
                    payload,
                    normalized_idempotency_key,
                    now,
                    now,
                ),
            )
            proposal_id = int(cur.lastrowid)
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="action_proposal",
                from_status=None,
                to_status="candidate",
                details="action_proposal_created",
                payload_json=json.dumps(
                    {
                        "proposal_id": proposal_id,
                        "proposal_type": normalized_type,
                        "destination": normalized_destination,
                    },
                    sort_keys=True,
                ),
                created_at=now,
            )
            row = conn.execute("SELECT * FROM action_proposals WHERE id = ?", (proposal_id,)).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("Failed to create action proposal.")
        return self._row_to_action_proposal(row)

    def get_action_proposal_by_idempotency_key(self, idempotency_key: str) -> ActionProposal | None:
        normalized = idempotency_key.strip()
        if not normalized:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM action_proposals WHERE idempotency_key = ?",
                (normalized,),
            ).fetchone()
        return self._row_to_action_proposal(row) if row is not None and _atlas_row_is_safe(row, _ACTION_FIELDS) else None

    def update_action_proposal_status(
        self,
        proposal_id: int,
        *,
        status: str,
        external_ref: str | None = None,
        exported_at: str | None = None,
        payload_json: dict[str, Any] | str | None = None,
    ) -> ActionProposal:
        normalized_status = status.strip().lower()
        if proposal_id <= 0:
            raise ValueError("proposal_id must be positive.")
        if normalized_status not in {"candidate", "approved", "rejected", "exported", "failed"}:
            raise ValueError("status must be one of: candidate, approved, rejected, exported, failed.")
        validate_persisted_metadata({"exported_at": exported_at})

        now = utc_now()
        payload = _json_or_none(payload_json)
        external_ref = _safe_text(external_ref)
        with self.connect() as conn:
            current = conn.execute("SELECT * FROM action_proposals WHERE id = ?", (proposal_id,)).fetchone()
            if current is None:
                raise ValueError(f"Action proposal {proposal_id} does not exist.")
            if not _atlas_row_is_safe(current, _ACTION_FIELDS):
                raise ValueError(f"Action proposal {proposal_id} contains unsafe persisted data.")
            final_exported_at = exported_at if exported_at is not None else current["exported_at"]
            if normalized_status == "exported" and final_exported_at is None:
                final_exported_at = now
            final_payload = payload if payload is not None else current["payload_json"]
            final_external_ref = external_ref if external_ref is not None else current["external_ref"]
            conn.execute(
                """
                UPDATE action_proposals
                SET status = ?,
                    external_ref = ?,
                    exported_at = ?,
                    payload_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (normalized_status, final_external_ref, final_exported_at, final_payload, now, proposal_id),
            )
            event_type = "action_export" if normalized_status == "exported" else "action_proposal"
            self._insert_event_row(
                conn,
                claim_id=current["claim_id"],
                event_type=event_type,
                from_status=current["status"],
                to_status=normalized_status,
                details="action_proposal_status_updated",
                payload_json=json.dumps(
                    {"proposal_id": proposal_id, "status": normalized_status},
                    sort_keys=True,
                ),
                created_at=now,
            )
            row = conn.execute("SELECT * FROM action_proposals WHERE id = ?", (proposal_id,)).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("Failed to update action proposal.")
        return self._row_to_action_proposal(row)

    def set_source_item_sensitivity(
        self,
        source_item_row_id: int,
        sensitivity: str | None,
    ) -> SourceItem:
        if source_item_row_id <= 0:
            raise ValueError("source_item_row_id must be positive.")
        normalized = _normalize_sensitivity(sensitivity)
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM source_items WHERE id = ?", (source_item_row_id,)).fetchone()
            if row is None:
                raise ValueError(f"Source item {source_item_row_id} does not exist.")
            if not _atlas_row_is_safe(row, _SOURCE_FIELDS):
                raise ValueError(f"Source item {source_item_row_id} contains unsafe persisted data.")
            current = row["sensitivity"] if "sensitivity" in row.keys() else None
            if current == normalized:
                return self._row_to_source_item(row)
            conn.execute(
                "UPDATE source_items SET sensitivity = ?, updated_at = ? WHERE id = ?",
                (normalized, now, source_item_row_id),
            )
            self._insert_event_row(
                conn,
                claim_id=None,
                event_type="source_import",
                from_status=None,
                to_status=None,
                details="source_item_sensitivity_set",
                payload_json=json.dumps(
                    {"source_item_id": source_item_row_id, "from": current, "to": normalized},
                    sort_keys=True,
                ),
                created_at=now,
            )
            updated = conn.execute("SELECT * FROM source_items WHERE id = ?", (source_item_row_id,)).fetchone()
            conn.commit()
        return self._row_to_source_item(updated)

    def set_evidence_item_sensitivity(
        self,
        evidence_item_row_id: int,
        sensitivity: str | None,
    ) -> EvidenceItem:
        if evidence_item_row_id <= 0:
            raise ValueError("evidence_item_row_id must be positive.")
        normalized = _normalize_sensitivity(sensitivity)
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM evidence_items WHERE id = ?", (evidence_item_row_id,)).fetchone()
            if row is None:
                raise ValueError(f"Evidence item {evidence_item_row_id} does not exist.")
            if not _atlas_row_is_safe(row, _EVIDENCE_FIELDS):
                raise ValueError(f"Evidence item {evidence_item_row_id} contains unsafe persisted data.")
            current = row["sensitivity"] if "sensitivity" in row.keys() else None
            if current == normalized:
                return self._row_to_evidence_item(row)
            conn.execute(
                "UPDATE evidence_items SET sensitivity = ? WHERE id = ?",
                (normalized, evidence_item_row_id),
            )
            self._insert_event_row(
                conn,
                claim_id=None,
                event_type="media_process",
                from_status=None,
                to_status=None,
                details="evidence_item_sensitivity_set",
                payload_json=json.dumps(
                    {"evidence_item_id": evidence_item_row_id, "from": current, "to": normalized},
                    sort_keys=True,
                ),
                created_at=now,
            )
            updated = conn.execute("SELECT * FROM evidence_items WHERE id = ?", (evidence_item_row_id,)).fetchone()
            conn.commit()
        return self._row_to_evidence_item(updated)

    # ----------------------------------------------------------------------
    # Media retry queue (Atlas v1.4.0)
    #
    # Architecture: MemoryMaster owns DURABLE STATE; LifeAgent/wacli owns the
    # actual HTTP fetch. process-media-retry-queue claims pending rows by
    # transitioning them to 'retrying' so LifeAgent picks them up; LifeAgent
    # then calls record-media-retry-outcome with the result.
    # ----------------------------------------------------------------------

    def enqueue_media_retry(
        self,
        *,
        source_item_id: int,
        media_key: str,
        chat_id: str | None = None,
        media_type: str | None = None,
        media_path: str | None = None,
        media_url: str | None = None,
        status: str = "pending",
        next_attempt_time: str | None = None,
    ) -> MediaRetryItem:
        """Enqueue (or re-enqueue) a media-retry row.

        Idempotent on (source_item_id, media_key) — a second call updates the
        existing row's metadata WITHOUT clobbering attempt_count/status.
        Use record_media_retry_outcome to advance state.
        """
        if source_item_id <= 0:
            raise ValueError("source_item_id must be positive.")
        normalized_key = (media_key or "").strip()
        validate_persisted_metadata({"media_key": normalized_key, "status": status, "next_attempt_time": next_attempt_time})
        chat_id, media_type, media_path, media_url = map(_safe_text, (chat_id, media_type, media_path, media_url))
        if not normalized_key:
            raise ValueError("media_key must be non-empty.")
        if status not in MEDIA_RETRY_STATUSES:
            raise ValueError(
                f"status must be one of: {', '.join(MEDIA_RETRY_STATUSES)}."
            )
        now = utc_now()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM media_retry_queue WHERE source_item_id = ? AND media_key = ?",
                (source_item_id, normalized_key),
            ).fetchone()
            if existing is not None:
                if not _atlas_row_is_safe(existing, _RETRY_FIELDS):
                    raise ValueError("Existing media retry contains unsafe persisted data.")
                # Update metadata only (do NOT clobber attempt_count/status).
                conn.execute(
                    """
                    UPDATE media_retry_queue
                    SET chat_id = COALESCE(?, chat_id),
                        media_type = COALESCE(?, media_type),
                        media_path = COALESCE(?, media_path),
                        media_url = COALESCE(?, media_url),
                        next_attempt_time = COALESCE(?, next_attempt_time),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (chat_id, media_type, media_path, media_url, next_attempt_time, now, int(existing["id"])),
                )
                row = conn.execute("SELECT * FROM media_retry_queue WHERE id = ?", (int(existing["id"]),)).fetchone()
                conn.commit()
                return self._row_to_media_retry(row)
            cur = conn.execute(
                """
                INSERT INTO media_retry_queue (
                    source_item_id, media_key, chat_id, media_type, media_path, media_url,
                    status, attempt_count, last_http_status, last_error, next_attempt_time,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?)
                """,
                (
                    source_item_id, normalized_key, chat_id, media_type, media_path, media_url,
                    status, next_attempt_time, now, now,
                ),
            )
            retry_id = int(cur.lastrowid)
            self._insert_event_row(
                conn,
                claim_id=None,
                event_type="media_process",
                from_status=None,
                to_status=status,
                details="media_retry_enqueued",
                payload_json=json.dumps(
                    {"retry_id": retry_id, "source_item_id": source_item_id, "media_key": normalized_key},
                    sort_keys=True,
                ),
                created_at=now,
            )
            row = conn.execute("SELECT * FROM media_retry_queue WHERE id = ?", (retry_id,)).fetchone()
            conn.commit()
        return self._row_to_media_retry(row)

    def claim_pending_media_retries(self, limit: int = 25) -> list[MediaRetryItem]:
        """Atomically claim up to N pending rows whose next_attempt_time is past.

        Transitions claimed rows from 'pending' to 'retrying' and increments
        attempt_count. Returns the claimed rows so LifeAgent can fetch them.
        """
        if limit <= 0:
            return []
        now = utc_now()
        with self.connect() as conn:
            # BEGIN IMMEDIATE takes the write lock up front so the SELECT and
            # the claiming UPDATE are one atomic read-modify-write. Without it,
            # two concurrent fetchers can SELECT the same pending rows, both
            # UPDATE (double-incrementing attempt_count, emitting duplicate
            # events) and both receive the same media_key — breaking the
            # single-claimer guarantee. The loser waits on busy_timeout instead
            # of failing. The "AND status = 'pending'" on the UPDATE is the
            # second guard: a row already moved out of 'pending' is never
            # re-claimed even if a stale id slips through.
            conn.execute("BEGIN IMMEDIATE")
            ids: list[int] = []
            cursor_id = 0
            page_size = min(max(limit, 25), 250)
            while len(ids) < limit:
                rows = conn.execute(
                    """SELECT * FROM media_retry_queue
                       WHERE status = 'pending' AND id > ?
                         AND (next_attempt_time IS NULL OR next_attempt_time <= ?)
                       ORDER BY id ASC LIMIT ?""",
                    (cursor_id, now, page_size),
                ).fetchall()
                if not rows:
                    break
                ids.extend(
                    int(row["id"])
                    for row in rows
                    if _atlas_row_is_safe(row, _RETRY_FIELDS)
                )
                ids = ids[:limit]
                cursor_id = int(rows[-1]["id"])
            if not ids:
                conn.commit()
                return []
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE media_retry_queue
                SET status = 'retrying',
                    attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE status = 'pending'
                  AND id IN ({placeholders})
                """,
                [now, *ids],
            )
            for retry_id in ids:
                self._insert_event_row(
                    conn,
                    claim_id=None,
                    event_type="media_process",
                    from_status="pending",
                    to_status="retrying",
                    details="media_retry_claimed",
                    payload_json=json.dumps(
                        {"retry_id": retry_id},
                        sort_keys=True,
                    ),
                    created_at=now,
                )
            updated = conn.execute(
                f"SELECT * FROM media_retry_queue WHERE id IN ({placeholders}) ORDER BY id ASC",
                ids,
            ).fetchall()
            conn.commit()
        return [self._row_to_media_retry(r) for r in updated]

    def record_media_retry_outcome(
        self,
        retry_id: int,
        *,
        status: str,
        media_path: str | None = None,
        last_http_status: int | None = None,
        last_error: str | None = None,
        next_attempt_time: str | None = None,
    ) -> MediaRetryItem:
        """Record the outcome of LifeAgent's fetch attempt.

        Status semantics:
        - 'done': success. media_path is required.
        - 'expired': terminal — WhatsApp returned 403/410 or similar.
        - 'failed': non-terminal failure but LifeAgent gave up (max attempts).
        - 'pending': transient failure, retry later (set next_attempt_time).
        - 'retrying': uncommon — generally set by claim_pending; pass it back
          if LifeAgent wants to keep ownership without yet declaring outcome.
        """
        if retry_id <= 0:
            raise ValueError("retry_id must be positive.")
        if status not in MEDIA_RETRY_STATUSES:
            raise ValueError(
                f"status must be one of: {', '.join(MEDIA_RETRY_STATUSES)}."
            )
        if status == "done" and not media_path:
            raise ValueError("media_path is required when status='done'.")
        media_path, last_error = map(_safe_text, (media_path, last_error))
        validate_persisted_metadata({"next_attempt_time": next_attempt_time, "status": status})
        now = utc_now()
        with self.connect() as conn:
            current = conn.execute(
                "SELECT * FROM media_retry_queue WHERE id = ?", (retry_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"media_retry_queue row {retry_id} does not exist.")
            if not _atlas_row_is_safe(current, _RETRY_FIELDS):
                raise ValueError(f"media_retry_queue row {retry_id} contains unsafe persisted data.")
            new_path = media_path if media_path is not None else current["media_path"]
            new_http = last_http_status if last_http_status is not None else current["last_http_status"]
            new_err = last_error if last_error is not None else current["last_error"]
            new_next = next_attempt_time if next_attempt_time is not None else current["next_attempt_time"]
            conn.execute(
                """
                UPDATE media_retry_queue
                SET status = ?,
                    media_path = ?,
                    last_http_status = ?,
                    last_error = ?,
                    next_attempt_time = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, new_path, new_http, new_err, new_next, now, retry_id),
            )
            self._insert_event_row(
                conn,
                claim_id=None,
                event_type="media_process",
                from_status=current["status"],
                to_status=status,
                details=f"media_retry_outcome_{status}",
                payload_json=json.dumps(
                    {
                        "retry_id": retry_id,
                        "http_status": last_http_status,
                        "has_path": bool(new_path),
                    },
                    sort_keys=True,
                ),
                created_at=now,
            )
            row = conn.execute("SELECT * FROM media_retry_queue WHERE id = ?", (retry_id,)).fetchone()
            conn.commit()
        return self._row_to_media_retry(row)

    def list_media_retries(
        self,
        *,
        status: str | None = None,
        source_item_id: int | None = None,
        limit: int = 100,
    ) -> list[MediaRetryItem]:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            if status not in MEDIA_RETRY_STATUSES:
                raise ValueError(
                    f"status must be one of: {', '.join(MEDIA_RETRY_STATUSES)}."
                )
            clauses.append("status = ?")
            params.append(status)
        if source_item_id is not None:
            if source_item_id <= 0:
                raise ValueError("source_item_id must be positive.")
            clauses.append("source_item_id = ?")
            params.append(source_item_id)
        if limit <= 0:
            return []
        page_size = min(max(limit, 25), 250)
        results: list[MediaRetryItem] = []
        cursor: tuple[object, int] | None = None
        with self.connect() as conn:
            while len(results) < limit:
                page_clauses = list(clauses)
                page_params = list(params)
                if cursor is not None:
                    page_clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
                    page_params.extend((cursor[0], cursor[0], cursor[1]))
                where_sql = f"WHERE {' AND '.join(page_clauses)}" if page_clauses else ""
                rows = conn.execute(
                    f"SELECT * FROM media_retry_queue {where_sql} ORDER BY updated_at DESC, id DESC LIMIT ?",
                    [*page_params, page_size],
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    if _atlas_row_is_safe(row, _RETRY_FIELDS):
                        results.append(self._row_to_media_retry(row))
                        if len(results) == limit:
                            break
                cursor = (rows[-1]["updated_at"], int(rows[-1]["id"]))
        return results

    def media_retry_status_counts(self) -> dict[str, int]:
        """Return {status: count} aggregated across the queue."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM media_retry_queue GROUP BY status"
            ).fetchall()
        counts = {s: 0 for s in MEDIA_RETRY_STATUSES}
        for r in rows:
            counts[str(r["status"])] = int(r["n"])
        return counts

    @staticmethod
    def _row_to_media_retry(row: sqlite3.Row) -> MediaRetryItem:
        return MediaRetryItem(
            id=int(row["id"]),
            source_item_id=int(row["source_item_id"]),
            media_key=str(row["media_key"]),
            chat_id=row["chat_id"],
            media_type=row["media_type"],
            media_path=row["media_path"],
            media_url=row["media_url"],
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            last_http_status=int(row["last_http_status"]) if row["last_http_status"] is not None else None,
            last_error=row["last_error"],
            next_attempt_time=row["next_attempt_time"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def update_action_proposal_fields(
        self,
        proposal_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        suggested_due_at: str | None = None,
        confidence: float | None = None,
        payload_json: dict[str, Any] | str | None = None,
    ) -> ActionProposal:
        """Edit user-facing proposal fields without touching status/lifecycle.

        Each kwarg is OPT-IN: pass ``None`` (or omit) to leave the field
        unchanged. Use ``update_action_proposal_status`` for state transitions.
        Status/external_ref/exported_at/claim_id/source_item_id/evidence_item_id
        are intentionally NOT editable here — those are lifecycle/structural
        fields that have their own paths.
        """
        if proposal_id <= 0:
            raise ValueError("proposal_id must be positive.")
        if title is None and description is None and suggested_due_at is None and confidence is None and payload_json is None:
            raise ValueError("at least one field must be provided to update.")
        validate_persisted_metadata({"suggested_due_at": suggested_due_at})

        normalized_title = title.strip() if title is not None else None
        normalized_title = _safe_text(normalized_title)
        description = _safe_text(description)
        if normalized_title is not None and not normalized_title:
            raise ValueError("title cannot be blank when provided.")
        bounded = _bounded_confidence(confidence) if confidence is not None else None
        payload = _json_or_none(payload_json) if payload_json is not None else None

        now = utc_now()
        with self.connect() as conn:
            current = conn.execute("SELECT * FROM action_proposals WHERE id = ?", (proposal_id,)).fetchone()
            if current is None:
                raise ValueError(f"Action proposal {proposal_id} does not exist.")
            if not _atlas_row_is_safe(current, _ACTION_FIELDS):
                raise ValueError(f"Action proposal {proposal_id} contains unsafe persisted data.")

            updates: list[str] = []
            params: list[object] = []
            changed: dict[str, object] = {}
            if normalized_title is not None and normalized_title != current["title"]:
                updates.append("title = ?")
                params.append(normalized_title)
                changed["title"] = normalized_title
            if description is not None and description != current["description"]:
                updates.append("description = ?")
                params.append(description)
                changed["description"] = description
            if suggested_due_at is not None and suggested_due_at != current["suggested_due_at"]:
                updates.append("suggested_due_at = ?")
                params.append(suggested_due_at)
                changed["suggested_due_at"] = suggested_due_at
            if bounded is not None and bounded != current["confidence"]:
                updates.append("confidence = ?")
                params.append(bounded)
                changed["confidence"] = bounded
            if payload is not None and payload != current["payload_json"]:
                updates.append("payload_json = ?")
                params.append(payload)
                changed["payload_json_updated"] = True

            if not updates:
                # No-op update — return current row, do not record event.
                return self._row_to_action_proposal(current)

            updates.append("updated_at = ?")
            params.append(now)
            params.append(proposal_id)

            conn.execute(
                f"UPDATE action_proposals SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            self._insert_event_row(
                conn,
                claim_id=current["claim_id"],
                event_type="action_proposal",
                from_status=current["status"],
                to_status=current["status"],
                details="action_proposal_fields_updated",
                payload_json=json.dumps(
                    {"proposal_id": proposal_id, "changed": list(changed.keys())},
                    sort_keys=True,
                ),
                created_at=now,
            )
            row = conn.execute("SELECT * FROM action_proposals WHERE id = ?", (proposal_id,)).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("Failed to update action proposal fields.")
        return self._row_to_action_proposal(row)

    def list_action_proposals(
        self,
        *,
        status: str | None = None,
        destination: str | None = None,
        limit: int = 100,
    ) -> list[ActionProposal]:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status.strip().lower())
        if destination:
            clauses.append("destination = ?")
            params.append(destination.strip())
        if limit <= 0:
            return []
        page_size = min(max(limit, 25), 250)
        results: list[ActionProposal] = []
        cursor: tuple[object, int] | None = None
        with self.connect() as conn:
            while len(results) < limit:
                page_clauses = list(clauses)
                page_params = list(params)
                if cursor is not None:
                    page_clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
                    page_params.extend((cursor[0], cursor[0], cursor[1]))
                where_sql = f"WHERE {' AND '.join(page_clauses)}" if page_clauses else ""
                rows = conn.execute(
                    f"SELECT * FROM action_proposals {where_sql} ORDER BY updated_at DESC, id DESC LIMIT ?",
                    [*page_params, page_size],
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    if _atlas_row_is_safe(row, _ACTION_FIELDS):
                        results.append(self._row_to_action_proposal(row))
                        if len(results) == limit:
                            break
                cursor = (rows[-1]["updated_at"], int(rows[-1]["id"]))
        return results

    @staticmethod
    def _row_to_external_source(row: sqlite3.Row) -> ExternalSource:
        return ExternalSource(
            id=int(row["id"]),
            source_type=str(row["source_type"]),
            display_name=str(row["display_name"]),
            config_json=row["config_json"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_source_item(row: sqlite3.Row) -> SourceItem:
        return SourceItem(
            id=int(row["id"]),
            source_id=int(row["source_id"]),
            source_item_id=str(row["source_item_id"]),
            item_type=str(row["item_type"]),
            chat_id=row["chat_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            occurred_at=row["occurred_at"],
            text=row["text"],
            payload_json=row["payload_json"],
            content_hash=row["content_hash"],
            sensitivity=row["sensitivity"] if "sensitivity" in row.keys() else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_evidence_item(row: sqlite3.Row) -> EvidenceItem:
        confidence = row["confidence"]
        return EvidenceItem(
            id=int(row["id"]),
            source_item_id=int(row["source_item_id"]),
            evidence_type=str(row["evidence_type"]),
            text=row["text"],
            media_path=row["media_path"],
            provider=row["provider"],
            confidence=float(confidence) if confidence is not None else None,
            payload_json=row["payload_json"],
            sensitivity=row["sensitivity"] if "sensitivity" in row.keys() else None,
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_to_action_proposal(row: sqlite3.Row) -> ActionProposal:
        return ActionProposal(
            id=int(row["id"]),
            proposal_type=str(row["proposal_type"]),
            title=str(row["title"]),
            description=row["description"],
            source_item_id=int(row["source_item_id"]) if row["source_item_id"] is not None else None,
            evidence_item_id=int(row["evidence_item_id"]) if row["evidence_item_id"] is not None else None,
            claim_id=int(row["claim_id"]) if row["claim_id"] is not None else None,
            suggested_due_at=row["suggested_due_at"],
            destination=str(row["destination"]),
            status=str(row["status"]),
            confidence=float(row["confidence"]),
            payload_json=row["payload_json"],
            exported_at=row["exported_at"],
            external_ref=row["external_ref"],
            idempotency_key=row["idempotency_key"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
