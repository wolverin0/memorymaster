"""Producer contracts: producers fetch/authenticate; MemoryMaster normalizes evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, ClassVar, Protocol

from memorymaster.capture.adapters import CaptureEnvelope, InlineTextAdapter


@dataclass(frozen=True, slots=True)
class ProducerItem:
    external_id: str
    text: str
    source_uri: str | None = None
    content_hash: str | None = None
    metadata: dict[str, Any] | None = None


class CaptureProducer(Protocol):
    producer_name: str

    def normalize(self, item: ProducerItem) -> CaptureEnvelope:
        """Normalize fetched producer content without performing network I/O."""


class _TextProducer:
    producer_name: ClassVar[str]

    def normalize(self, item: ProducerItem) -> CaptureEnvelope:
        envelope = InlineTextAdapter(item.text, item.source_uri).capture()
        if item.content_hash:
            envelope = replace(envelope, content_hash=item.content_hash)
        return replace(envelope, source_kind=self.producer_name)


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
