"""Atlas Inbox source/evidence/action proposal storage methods.

This is a mixin class for memorymaster.storage.SQLiteStore. Methods expect
``self.connect()``, ``self._insert_event_row()``, and schema initialization from
``_SchemaMixin``.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from memorymaster._storage_shared import utc_now
from memorymaster.models import ActionProposal, EvidenceItem, ExternalSource, SourceItem


def _json_or_none(value: dict[str, Any] | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _bounded_confidence(confidence: float | None) -> float | None:
    if confidence is None:
        return None
    return max(0.0, min(1.0, float(confidence)))


class _SourceItemsMixin:
    def upsert_external_source(
        self,
        *,
        source_type: str,
        display_name: str,
        config_json: dict[str, Any] | str | None = None,
    ) -> ExternalSource:
        normalized_source_type = source_type.strip().lower()
        normalized_display_name = display_name.strip()
        if not normalized_source_type:
            raise ValueError("source_type must be non-empty.")
        if not normalized_display_name:
            raise ValueError("display_name must be non-empty.")

        now = utc_now()
        payload = _json_or_none(config_json)
        with self.connect() as conn:
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
            conn.commit()
        if row is None:
            raise RuntimeError("Failed to upsert external source.")
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
    ) -> SourceItem:
        normalized_source_item_id = source_item_id.strip()
        normalized_item_type = item_type.strip().lower()
        if source_id <= 0:
            raise ValueError("source_id must be positive.")
        if not normalized_source_item_id:
            raise ValueError("source_item_id must be non-empty.")
        if not normalized_item_type:
            raise ValueError("item_type must be non-empty.")

        now = utc_now()
        payload = _json_or_none(payload_json)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM source_items WHERE source_id = ? AND source_item_id = ?",
                (source_id, normalized_source_item_id),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO source_items (
                    source_id, source_item_id, item_type, chat_id, sender_id, sender_name,
                    occurred_at, text, payload_json, content_hash, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, source_item_id) DO UPDATE SET
                    item_type = excluded.item_type,
                    chat_id = excluded.chat_id,
                    sender_id = excluded.sender_id,
                    sender_name = excluded.sender_name,
                    occurred_at = excluded.occurred_at,
                    text = excluded.text,
                    payload_json = excluded.payload_json,
                    content_hash = excluded.content_hash,
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
            conn.commit()
        if row is None:
            raise RuntimeError("Failed to upsert source item.")
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
        return self._row_to_source_item(row) if row is not None else None

    def get_source_item_by_id(self, source_item_row_id: int) -> SourceItem | None:
        if source_item_row_id <= 0:
            raise ValueError("source_item_row_id must be positive.")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM source_items WHERE id = ?", (source_item_row_id,)).fetchone()
        return self._row_to_source_item(row) if row is not None else None

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
    ) -> EvidenceItem:
        normalized_evidence_type = evidence_type.strip().lower()
        if source_item_id <= 0:
            raise ValueError("source_item_id must be positive.")
        if not normalized_evidence_type:
            raise ValueError("evidence_type must be non-empty.")

        now = utc_now()
        payload = _json_or_none(payload_json)
        bounded = _bounded_confidence(confidence)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO evidence_items (
                    source_item_id, evidence_type, text, media_path, provider,
                    confidence, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source_item_id, normalized_evidence_type, text, media_path, provider, bounded, payload, now),
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
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM evidence_items {where_sql} ORDER BY created_at ASC, id ASC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_evidence_item(row) for row in rows]

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
        normalized_type = proposal_type.strip().lower()
        normalized_title = title.strip()
        normalized_destination = destination.strip() or "manual"
        normalized_idempotency_key = (idempotency_key or "").strip() or None
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
        return self._row_to_action_proposal(row) if row is not None else None

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

        now = utc_now()
        payload = _json_or_none(payload_json)
        with self.connect() as conn:
            current = conn.execute("SELECT * FROM action_proposals WHERE id = ?", (proposal_id,)).fetchone()
            if current is None:
                raise ValueError(f"Action proposal {proposal_id} does not exist.")
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
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM action_proposals {where_sql} ORDER BY updated_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_action_proposal(row) for row in rows]

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
