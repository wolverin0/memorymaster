from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def _service(prefix: str = "atlas-source") -> MemoryService:
    service = MemoryService(_case_db(prefix), workspace_root=Path.cwd())
    service.init_db()
    return service


def test_external_source_upsert_is_idempotent() -> None:
    service = _service("atlas-source-upsert")

    first = service.upsert_external_source(
        source_type="WhatsApp",
        display_name="primary",
        config_json={"mode": "wacli"},
    )
    second = service.upsert_external_source(
        source_type="whatsapp",
        display_name="primary",
        config_json={"mode": "wacli", "updated": True},
    )

    assert first.id == second.id
    assert second.source_type == "whatsapp"
    assert json.loads(second.config_json or "{}")["updated"] is True


def test_source_item_upsert_dedupes_and_records_import_event() -> None:
    service = _service("atlas-source-item-upsert")
    source = service.upsert_external_source(source_type="whatsapp", display_name="primary")

    first = service.upsert_source_item(
        source_id=source.id,
        source_item_id="msg-1",
        item_type="message",
        chat_id="chat-1",
        sender_id="user-1",
        sender_name="Laura",
        occurred_at="2026-05-05T03:00:00+00:00",
        text="Please remind me tomorrow",
        payload_json={"message_type": "text"},
        content_hash="hash-1",
    )
    second = service.upsert_source_item(
        source_id=source.id,
        source_item_id="msg-1",
        item_type="message",
        chat_id="chat-1",
        sender_id="user-1",
        sender_name="Laura P.",
        occurred_at="2026-05-05T03:00:00+00:00",
        text="Please remind me tomorrow",
        payload_json={"message_type": "text", "edited": True},
        content_hash="hash-1",
    )

    assert first.id == second.id
    assert second.sender_name == "Laura P."
    assert json.loads(second.payload_json or "{}")["edited"] is True

    events = service.list_events(event_type="source_import")
    assert len(events) == 1
    assert events[0].details == "source_item_imported"


def test_evidence_item_links_to_source_item_and_records_event() -> None:
    service = _service("atlas-evidence")
    source = service.upsert_external_source(source_type="whatsapp", display_name="primary")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="msg-audio-1",
        item_type="audio",
        text=None,
        payload_json={"media_type": "audio"},
    )

    evidence = service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="transcript",
        text="The router stopped working yesterday",
        media_path="media/chat/msg-audio-1/audio.ogg",
        provider="mock-whisper",
        confidence=0.87,
    )

    assert evidence.source_item_id == item.id
    assert evidence.evidence_type == "transcript"
    assert evidence.confidence == pytest.approx(0.87)

    events = service.list_events(event_type="media_process")
    assert len(events) == 1
    assert events[0].details == "evidence_item_added"


def test_action_proposal_lifecycle_is_reviewable_and_audited() -> None:
    service = _service("atlas-action-proposal")
    source = service.upsert_external_source(source_type="whatsapp", display_name="primary")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="msg-task-1",
        item_type="message",
        text="Can you send me the installation quote tomorrow?",
    )
    evidence = service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="message_text",
        text=item.text,
    )
    claim = service.ingest(
        text="A contact requested an installation quote tomorrow.",
        citations=[
            CitationInput(
                source="whatsapp://primary/chat/msg-task-1",
                locator="msg-task-1",
                excerpt=item.text,
            )
        ],
        claim_type="fact",
        subject="contact",
        predicate="requested",
        object_value="installation quote tomorrow",
    )

    proposal = service.create_action_proposal(
        proposal_type="task",
        title="Send installation quote",
        description="Source-backed task extracted from WhatsApp.",
        source_item_id=item.id,
        evidence_item_id=evidence.id,
        claim_id=claim.id,
        suggested_due_at="2026-05-06T12:00:00+00:00",
        destination="super-productivity",
        confidence=0.81,
        idempotency_key="msg-task-1:task:quote",
    )
    duplicate = service.create_action_proposal(
        proposal_type="task",
        title="Duplicate should not win",
        idempotency_key="msg-task-1:task:quote",
    )

    assert duplicate.id == proposal.id
    assert duplicate.title == proposal.title

    approved = service.update_action_proposal_status(proposal.id, status="approved")
    exported = service.update_action_proposal_status(
        proposal.id,
        status="exported",
        external_ref="sp-task-1",
    )

    assert approved.status == "approved"
    assert exported.status == "exported"
    assert exported.external_ref == "sp-task-1"
    assert exported.exported_at is not None

    exported_rows = service.list_action_proposals(status="exported", destination="super-productivity")
    assert [row.id for row in exported_rows] == [proposal.id]

    proposal_events = service.list_events(event_type="action_proposal")
    export_events = service.list_events(event_type="action_export")
    assert len(proposal_events) >= 2
    assert len(export_events) == 1
