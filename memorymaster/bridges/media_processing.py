"""Media processing interfaces for Atlas Inbox evidence extraction."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from memorymaster.core.models import EvidenceItem


@dataclass(frozen=True)
class EvidenceResult:
    evidence_type: str
    text: str
    provider: str
    media_path: str | None = None
    confidence: float | None = None
    payload_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class MediaProcessOutcome:
    source_item_id: int
    evidence: EvidenceItem | None
    created: bool
    error: str | None = None


class TranscriptionProvider(Protocol):
    provider_name: str

    def transcribe(self, path: str) -> EvidenceResult:
        """Return transcript evidence for an audio/video path."""


class OcrProvider(Protocol):
    provider_name: str

    def extract(self, path: str) -> EvidenceResult:
        """Return OCR evidence for an image/document path."""


class MockTranscriptionProvider:
    provider_name = "mock-transcription"

    def __init__(self, outputs: dict[str, str] | None = None) -> None:
        self.outputs = outputs or {}

    def transcribe(self, path: str) -> EvidenceResult:
        text = self.outputs.get(path) or f"Mock transcript for {Path(path).name}"
        return EvidenceResult(
            evidence_type="transcript",
            text=text,
            provider=self.provider_name,
            media_path=path,
            confidence=0.99,
        )


class MockOcrProvider:
    provider_name = "mock-ocr"

    def __init__(self, outputs: dict[str, str] | None = None) -> None:
        self.outputs = outputs or {}

    def extract(self, path: str) -> EvidenceResult:
        text = self.outputs.get(path) or f"Mock OCR for {Path(path).name}"
        return EvidenceResult(
            evidence_type="ocr",
            text=text,
            provider=self.provider_name,
            media_path=path,
            confidence=0.99,
        )


def process_transcription(
    service,
    source_item_row_id: int,
    provider: TranscriptionProvider,
) -> MediaProcessOutcome:
    return _process_media(
        service,
        source_item_row_id,
        evidence_type="transcript",
        provider_name=provider.provider_name,
        processor=provider.transcribe,
    )


def process_ocr(
    service,
    source_item_row_id: int,
    provider: OcrProvider,
) -> MediaProcessOutcome:
    return _process_media(
        service,
        source_item_row_id,
        evidence_type="ocr",
        provider_name=provider.provider_name,
        processor=provider.extract,
    )


def _process_media(
    service,
    source_item_row_id: int,
    *,
    evidence_type: str,
    provider_name: str,
    processor,
) -> MediaProcessOutcome:
    source_item = service.get_source_item_by_id(source_item_row_id)
    if source_item is None:
        raise ValueError(f"Source item {source_item_row_id} does not exist.")

    existing = service.list_evidence_items(
        source_item_id=source_item.id,
        evidence_type=evidence_type,
        limit=1,
    )
    if existing:
        return MediaProcessOutcome(source_item_id=source_item.id, evidence=existing[0], created=False)

    media_path = _extract_media_path(source_item.payload_json)
    try:
        result = processor(media_path)
    except Exception as exc:
        _record_media_failure(
            service,
            source_item_id=source_item.id,
            evidence_type=evidence_type,
            provider=provider_name,
            media_path=media_path,
            error=str(exc),
        )
        return MediaProcessOutcome(
            source_item_id=source_item.id,
            evidence=None,
            created=False,
            error=str(exc),
        )

    evidence = service.add_evidence_item(
        source_item_id=source_item.id,
        evidence_type=result.evidence_type,
        text=result.text,
        media_path=result.media_path or media_path,
        provider=result.provider,
        confidence=result.confidence,
        payload_json=result.payload_json,
    )
    return MediaProcessOutcome(source_item_id=source_item.id, evidence=evidence, created=True)


def _extract_media_path(payload_json: str | None) -> str:
    if not payload_json:
        return ""
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("media_path", "file_path", "local_path", "path", "url"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    media = payload.get("media")
    if isinstance(media, dict):
        for key in ("path", "file_path", "local_path", "url"):
            value = media.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def _record_media_failure(
    service,
    *,
    source_item_id: int,
    evidence_type: str,
    provider: str,
    media_path: str,
    error: str,
) -> None:
    service.store.record_event(
        claim_id=None,
        event_type="media_process",
        details="media_process_failed",
        payload={
            "source_item_id": source_item_id,
            "evidence_type": evidence_type,
            "provider": provider,
            "media_path": media_path,
            "error": error,
        },
    )
