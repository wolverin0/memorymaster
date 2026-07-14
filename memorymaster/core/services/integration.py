"""Atlas/media/action persistence collaborator for ``MemoryService``."""

from __future__ import annotations

from typing import Any

from memorymaster.core.models import (
    ActionProposal,
    EvidenceItem,
    ExternalSource,
    MediaRetryItem,
    SourceItem,
)


class IntegrationService:
    """Store-backed integration API inherited by the compatibility facade.

    Subclasses provide ``store``. Keeping the implementations here preserves
    every public method signature while removing integration ownership from the
    authoritative orchestration module.
    """

    store: Any

    def upsert_external_source(
        self,
        *,
        source_type: str,
        display_name: str,
        config_json: dict[str, object] | str | None = None,
    ) -> ExternalSource:
        return self.store.upsert_external_source(
            source_type=source_type,
            display_name=display_name,
            config_json=config_json,
        )

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
        payload_json: dict[str, object] | str | None = None,
        content_hash: str | None = None,
        sensitivity: str | None = None,
    ) -> SourceItem:
        return self.store.upsert_source_item(
            source_id=source_id,
            source_item_id=source_item_id,
            item_type=item_type,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            occurred_at=occurred_at,
            text=text,
            payload_json=payload_json,
            content_hash=content_hash,
            sensitivity=sensitivity,
        )

    def get_source_item(self, *, source_id: int, source_item_id: str) -> SourceItem | None:
        return self.store.get_source_item(source_id=source_id, source_item_id=source_item_id)

    def get_source_item_by_id(self, source_item_row_id: int) -> SourceItem | None:
        return self.store.get_source_item_by_id(source_item_row_id)

    def add_evidence_item(
        self,
        *,
        source_item_id: int,
        evidence_type: str,
        text: str | None = None,
        media_path: str | None = None,
        provider: str | None = None,
        confidence: float | None = None,
        payload_json: dict[str, object] | str | None = None,
        sensitivity: str | None = None,
    ) -> EvidenceItem:
        return self.store.add_evidence_item(
            source_item_id=source_item_id,
            evidence_type=evidence_type,
            text=text,
            media_path=media_path,
            provider=provider,
            confidence=confidence,
            payload_json=payload_json,
            sensitivity=sensitivity,
        )

    def set_source_item_sensitivity(
        self, source_item_row_id: int, sensitivity: str | None
    ) -> SourceItem:
        return self.store.set_source_item_sensitivity(source_item_row_id, sensitivity)

    def set_evidence_item_sensitivity(
        self, evidence_item_row_id: int, sensitivity: str | None
    ) -> EvidenceItem:
        return self.store.set_evidence_item_sensitivity(evidence_item_row_id, sensitivity)

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
        return self.store.enqueue_media_retry(
            source_item_id=source_item_id,
            media_key=media_key,
            chat_id=chat_id,
            media_type=media_type,
            media_path=media_path,
            media_url=media_url,
            status=status,
            next_attempt_time=next_attempt_time,
        )

    def claim_pending_media_retries(
        self,
        limit: int = 25,
        *,
        lease_owner: str = "media-worker",
        lease_seconds: int = 300,
    ) -> list[MediaRetryItem]:
        return self.store.claim_pending_media_retries(
            limit=limit,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )

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
        return self.store.record_media_retry_outcome(
            retry_id,
            status=status,
            media_path=media_path,
            last_http_status=last_http_status,
            last_error=last_error,
            next_attempt_time=next_attempt_time,
        )

    def list_media_retries(
        self,
        *,
        status: str | None = None,
        source_item_id: int | None = None,
        limit: int = 100,
    ) -> list[MediaRetryItem]:
        return self.store.list_media_retries(
            status=status,
            source_item_id=source_item_id,
            limit=limit,
        )

    def media_retry_status_counts(self) -> dict[str, int]:
        return self.store.media_retry_status_counts()

    def list_evidence_items(
        self,
        *,
        source_item_id: int | None = None,
        evidence_type: str | None = None,
        limit: int = 100,
    ) -> list[EvidenceItem]:
        return self.store.list_evidence_items(
            source_item_id=source_item_id,
            evidence_type=evidence_type,
            limit=limit,
        )

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
        payload_json: dict[str, object] | str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionProposal:
        return self.store.create_action_proposal(
            proposal_type=proposal_type,
            title=title,
            description=description,
            source_item_id=source_item_id,
            evidence_item_id=evidence_item_id,
            claim_id=claim_id,
            suggested_due_at=suggested_due_at,
            destination=destination,
            confidence=confidence,
            payload_json=payload_json,
            idempotency_key=idempotency_key,
        )

    def update_action_proposal_status(
        self,
        proposal_id: int,
        *,
        status: str,
        external_ref: str | None = None,
        exported_at: str | None = None,
        payload_json: dict[str, object] | str | None = None,
    ) -> ActionProposal:
        return self.store.update_action_proposal_status(
            proposal_id,
            status=status,
            external_ref=external_ref,
            exported_at=exported_at,
            payload_json=payload_json,
        )

    def list_action_proposals(
        self,
        *,
        status: str | None = None,
        destination: str | None = None,
        limit: int = 100,
    ) -> list[ActionProposal]:
        return self.store.list_action_proposals(
            status=status,
            destination=destination,
            limit=limit,
        )

    def update_action_proposal_fields(
        self,
        proposal_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        suggested_due_at: str | None = None,
        confidence: float | None = None,
        payload_json: dict[str, object] | str | None = None,
    ) -> ActionProposal:
        return self.store.update_action_proposal_fields(
            proposal_id,
            title=title,
            description=description,
            suggested_due_at=suggested_due_at,
            confidence=confidence,
            payload_json=payload_json,
        )
