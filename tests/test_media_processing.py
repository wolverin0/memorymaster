from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.media_processing import (
    MockOcrProvider,
    MockTranscriptionProvider,
    process_ocr,
    process_transcription,
)
from memorymaster.service import MemoryService


class FailingTranscriptionProvider:
    provider_name = "failing-transcription"

    def transcribe(self, path: str):
        raise RuntimeError("transcription unavailable")


@pytest.fixture()
def service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "atlas.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def test_mock_transcription_creates_transcript_evidence(service: MemoryService) -> None:
    source = service.upsert_external_source(source_type="whatsapp", display_name="primary")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="voice-1",
        item_type="audio",
        payload_json={"media_path": "media/voice-1.ogg"},
    )
    provider = MockTranscriptionProvider({"media/voice-1.ogg": "Please call the client tomorrow"})

    first = process_transcription(service, item.id, provider)
    second = process_transcription(service, item.id, provider)

    assert first.created is True
    assert first.evidence is not None
    assert first.evidence.evidence_type == "transcript"
    assert first.evidence.text == "Please call the client tomorrow"
    assert second.created is False
    assert second.evidence is not None
    assert second.evidence.id == first.evidence.id


def test_mock_ocr_creates_ocr_evidence(service: MemoryService) -> None:
    source = service.upsert_external_source(source_type="whatsapp", display_name="primary")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="receipt-1",
        item_type="image",
        payload_json={"media": {"path": "media/receipt-1.jpg"}},
    )
    provider = MockOcrProvider({"media/receipt-1.jpg": "Receipt total ARS 12000"})

    outcome = process_ocr(service, item.id, provider)

    assert outcome.created is True
    assert outcome.evidence is not None
    assert outcome.evidence.evidence_type == "ocr"
    assert outcome.evidence.media_path == "media/receipt-1.jpg"


def test_media_processing_failure_records_audit_event(service: MemoryService) -> None:
    source = service.upsert_external_source(source_type="whatsapp", display_name="primary")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="voice-fail",
        item_type="audio",
        payload_json={"media_path": "missing.ogg"},
    )

    outcome = process_transcription(service, item.id, FailingTranscriptionProvider())

    assert outcome.created is False
    assert outcome.evidence is None
    assert outcome.error == "transcription unavailable"
    events = service.list_events(event_type="media_process")
    assert len(events) == 1
    assert events[0].details == "media_process_failed"
