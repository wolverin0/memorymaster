"""Producer contracts: producers fetch/authenticate; MemoryMaster normalizes evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re
from typing import Any, ClassVar, Protocol

from memorymaster.capture.adapters import CaptureEnvelope, CaptureRejected, InlineTextAdapter
from memorymaster.core.security import validate_persisted_metadata


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TURN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


@dataclass(frozen=True, slots=True)
class ProducerItem:
    external_id: str
    text: str
    source_uri: str | None = None
    content_hash: str | None = None
    session_hash: str | None = None
    turn_id: str | None = None
    metadata: dict[str, Any] | None = None


class CaptureProducer(Protocol):
    producer_name: str

    def normalize(self, item: ProducerItem) -> CaptureEnvelope:
        """Normalize fetched producer content without performing network I/O."""


class _TextProducer:
    producer_name: ClassVar[str]

    def normalize(self, item: ProducerItem) -> CaptureEnvelope:
        external_id_hash = _external_id_hash(item.external_id)
        envelope = InlineTextAdapter(item.text, item.source_uri).capture()
        if item.content_hash and item.content_hash != envelope.content_hash:
            raise CaptureRejected(
                "producer_hash_mismatch",
                "Producer content hash does not match the submitted evidence.",
            )
        session_hash = _optional_hash(item.session_hash, "producer_session_hash")
        turn_id = _optional_turn_id(item.turn_id)
        metadata = _producer_metadata(item.metadata)
        return replace(
            envelope,
            source_kind=self.producer_name,
            producer=self.producer_name,
            producer_external_id_hash=external_id_hash,
            producer_session_hash=session_hash,
            producer_turn_id=turn_id,
            producer_metadata=metadata,
        )


class HermesProducer(_TextProducer):
    producer_name = "hermes"


class WhatsAppProducer(_TextProducer):
    producer_name = "whatsapp"


class ObsidianClipperProducer(_TextProducer):
    producer_name = "obsidian-clipper"


class AgentProducer(_TextProducer):
    producer_name = "agent"


PRODUCERS: dict[str, CaptureProducer] = {
    producer.producer_name: producer
    for producer in (
        HermesProducer(),
        WhatsAppProducer(),
        ObsidianClipperProducer(),
        AgentProducer(),
    )
}


def normalize_producer_item(producer: str, item: ProducerItem) -> CaptureEnvelope:
    try:
        adapter = PRODUCERS[producer.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"Unknown capture producer: {producer}") from exc
    return adapter.normalize(item)


def _external_id_hash(value: str) -> str:
    identifier = str(value).strip()
    if not identifier or len(identifier.encode("utf-8")) > 512:
        raise CaptureRejected("producer_external_id_invalid", "Producer external ID is invalid.")
    try:
        validate_persisted_metadata({"producer_external_id": identifier})
    except ValueError as exc:
        raise CaptureRejected(
            "producer_external_id_sensitive",
            "Producer external ID contains sensitive data.",
        ) from exc
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()


def _optional_hash(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if _SHA256.fullmatch(normalized) is None:
        raise CaptureRejected(f"{field}_invalid", f"{field} must be a SHA-256 digest.")
    return normalized


def _optional_turn_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if _TURN_ID.fullmatch(normalized) is None:
        raise CaptureRejected("producer_turn_id_invalid", "Producer turn ID is invalid.")
    return normalized


def _producer_metadata(metadata: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    if metadata is None:
        return ()
    if len(metadata) > 20:
        raise CaptureRejected("producer_metadata_too_large", "Producer metadata has too many fields.")
    normalized = {str(key): str(value) for key, value in metadata.items()}
    if any(len(key) > 64 or len(value) > 256 for key, value in normalized.items()):
        raise CaptureRejected("producer_metadata_too_large", "Producer metadata is too large.")
    try:
        validate_persisted_metadata(normalized)
    except ValueError as exc:
        raise CaptureRejected(
            "producer_metadata_sensitive",
            "Producer metadata contains sensitive data.",
        ) from exc
    return tuple(sorted(normalized.items()))
