"""Rule-based Atlas action proposal extraction.

This is the deterministic first pass. It creates reviewable candidates only;
external systems are updated later after explicit approval/export.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Any

from memorymaster.models import ActionProposal, EvidenceItem, SourceItem


@dataclass(frozen=True)
class ActionExtractionResult:
    scanned: int
    matched: int
    created: int
    existing: int
    proposals: list[ActionProposal]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "matched": self.matched,
            "created": self.created,
            "existing": self.existing,
            "proposals": [asdict(proposal) for proposal in self.proposals],
        }


@dataclass(frozen=True)
class _ActionDraft:
    proposal_type: str
    title: str
    suggested_due_at: str | None
    confidence: float


_ACTION_MARKERS = (
    "can you",
    "could you",
    "please",
    "pls",
    "todo",
    "to do",
    "remind",
    "reminder",
    "follow up",
    "send",
    "pay",
    "call",
    "check",
    "verify",
    "quote",
    "budget",
    "recordame",
    "recuerdame",
    "mandame",
    "enviame",
    "pasame",
    "pagar",
    "llamar",
    "presupuesto",
    "cotizacion",
)


def propose_actions_from_evidence(
    service,
    *,
    destination: str = "super-productivity",
    limit: int = 200,
) -> ActionExtractionResult:
    evidence_items = service.list_evidence_items(limit=limit)
    scanned = 0
    matched = 0
    created = 0
    existing = 0
    proposals: list[ActionProposal] = []

    for evidence in evidence_items:
        scanned += 1
        source_item = service.get_source_item_by_id(evidence.source_item_id)
        draft = _draft_from_evidence(evidence, source_item)
        if draft is None:
            continue
        matched += 1
        idempotency_key = _proposal_idempotency_key(evidence, draft)
        before = service.get_action_proposal_by_idempotency_key(idempotency_key)
        proposal = service.create_action_proposal(
            proposal_type=draft.proposal_type,
            title=draft.title,
            description=_proposal_description(evidence, source_item),
            source_item_id=evidence.source_item_id,
            evidence_item_id=evidence.id,
            suggested_due_at=draft.suggested_due_at,
            destination=destination,
            confidence=draft.confidence,
            payload_json={
                "extractor": "atlas-rule-v1",
                "evidence_type": evidence.evidence_type,
                "source_item_external_id": source_item.source_item_id if source_item else None,
            },
            idempotency_key=idempotency_key,
        )
        if before is None:
            created += 1
        else:
            existing += 1
        proposals.append(proposal)

    return ActionExtractionResult(
        scanned=scanned,
        matched=matched,
        created=created,
        existing=existing,
        proposals=proposals,
    )


def _draft_from_evidence(evidence: EvidenceItem, source_item: SourceItem | None) -> _ActionDraft | None:
    text = (evidence.text or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if not any(marker in lowered for marker in _ACTION_MARKERS):
        return None

    proposal_type = "task"
    if "remind" in lowered or "recordame" in lowered or "recuerdame" in lowered or "reminder" in lowered:
        proposal_type = "reminder"
    elif "follow up" in lowered:
        proposal_type = "follow_up"

    title = _title_from_text(text)
    if not title:
        return None
    suggested_due_at = _suggested_due_at(lowered, source_item.occurred_at if source_item else None)
    confidence = 0.72
    if suggested_due_at is not None:
        confidence += 0.08
    if proposal_type != "task":
        confidence += 0.05
    return _ActionDraft(
        proposal_type=proposal_type,
        title=title,
        suggested_due_at=suggested_due_at,
        confidence=min(confidence, 0.9),
    )


def _title_from_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" .?!")
    cleaned = re.sub(
        r"(?i)^(hey|hola|che|please|pls|can you|could you|would you|remind me to|recordame que|recuerdame que)\s+",
        "",
        cleaned,
    )
    cleaned = re.sub(r"(?i)\b(tomorrow|today|tonight|manana)\b.*$", "", cleaned).strip(" .?!")
    if not cleaned:
        return ""
    cleaned = cleaned[:80].strip(" .?!")
    return cleaned[0].upper() + cleaned[1:]


def _suggested_due_at(lowered_text: str, occurred_at: str | None) -> str | None:
    base = _parse_datetime(occurred_at) or datetime.now().astimezone()
    if "tomorrow" in lowered_text or "manana" in lowered_text:
        due_date = base.date() + timedelta(days=1)
        return datetime.combine(due_date, time(hour=12), tzinfo=base.tzinfo).isoformat()
    if "today" in lowered_text or "tonight" in lowered_text:
        return datetime.combine(base.date(), time(hour=18), tzinfo=base.tzinfo).isoformat()
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _proposal_description(evidence: EvidenceItem, source_item: SourceItem | None) -> str:
    parts = ["Source-backed action proposal extracted from evidence."]
    if source_item is not None:
        if source_item.chat_id:
            parts.append(f"Chat: {source_item.chat_id}.")
        if source_item.sender_name or source_item.sender_id:
            parts.append(f"Sender: {source_item.sender_name or source_item.sender_id}.")
    excerpt = (evidence.text or "").strip()
    if excerpt:
        parts.append(f"Excerpt: {excerpt[:240]}")
    return " ".join(parts)


def _proposal_idempotency_key(evidence: EvidenceItem, draft: _ActionDraft) -> str:
    digest = hashlib.sha256(f"{draft.proposal_type}\n{draft.title}".encode("utf-8")).hexdigest()[:16]
    return f"evidence:{evidence.id}:{draft.proposal_type}:{digest}"
