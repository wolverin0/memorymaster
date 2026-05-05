"""Deterministic Atlas Inbox claim extraction from source evidence."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any

from memorymaster.models import CitationInput, Claim, EvidenceItem, SourceItem


@dataclass(frozen=True)
class AtlasClaimExtractionResult:
    scanned: int
    matched: int
    ingested: int
    claims: list[Claim]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "matched": self.matched,
            "ingested": self.ingested,
            "claims": [asdict(claim) for claim in self.claims],
        }


@dataclass(frozen=True)
class _ClaimDraft:
    text: str
    claim_type: str
    subject: str
    predicate: str
    object_value: str
    confidence: float


def extract_atlas_claims_from_evidence(
    service,
    *,
    scope: str | None = None,
    limit: int = 200,
) -> AtlasClaimExtractionResult:
    if scope is None:
        from pathlib import Path

        from memorymaster.scope_utils import scope_from_cwd

        scope = scope_from_cwd(Path.cwd())
    evidence_items = service.list_evidence_items(limit=limit)
    scanned = 0
    matched = 0
    ingested = 0
    claims: list[Claim] = []

    for evidence in evidence_items:
        scanned += 1
        source_item = service.get_source_item_by_id(evidence.source_item_id)
        draft = _draft_from_evidence(evidence)
        if draft is None:
            continue
        matched += 1
        claim = service.ingest(
            text=draft.text,
            citations=[_citation_for_evidence(evidence, source_item)],
            idempotency_key=_claim_idempotency_key(evidence, draft),
            claim_type=draft.claim_type,
            subject=draft.subject,
            predicate=draft.predicate,
            object_value=draft.object_value,
            scope=scope,
            confidence=draft.confidence,
            volatility="medium",
        )
        ingested += 1
        claims.append(claim)

    return AtlasClaimExtractionResult(scanned=scanned, matched=matched, ingested=ingested, claims=claims)


def _draft_from_evidence(evidence: EvidenceItem) -> _ClaimDraft | None:
    text = (evidence.text or "").strip()
    if not text:
        return None
    normalized = re.sub(r"\s+", " ", text).strip(" .?!")
    lowered = normalized.lower()

    if _contains_any(lowered, ("stopped working", "not working", "no internet", "down", "broken", "fallo", "no funciona")):
        object_value = normalized
        return _ClaimDraft(
            text=f"A WhatsApp contact reported a problem: {object_value}.",
            claim_type="problem",
            subject="whatsapp_contact",
            predicate="reported_problem",
            object_value=object_value,
            confidence=0.58,
        )

    if _contains_any(lowered, ("sent receipt", "sent the receipt", "payment proof", "comprobante", "recibo", "receipt")):
        object_value = normalized
        return _ClaimDraft(
            text=f"A WhatsApp contact mentioned payment or receipt evidence: {object_value}.",
            claim_type="payment_evidence",
            subject="whatsapp_contact",
            predicate="mentioned_payment_evidence",
            object_value=object_value,
            confidence=0.57,
        )

    if _contains_any(lowered, ("quote", "budget", "estimate", "presupuesto", "cotizacion")):
        object_value = _clean_request_object(normalized)
        return _ClaimDraft(
            text=f"A WhatsApp contact requested a quote or estimate: {object_value}.",
            claim_type="request",
            subject="whatsapp_contact",
            predicate="requested_quote",
            object_value=object_value,
            confidence=0.59,
        )

    if _contains_any(lowered, ("can you", "could you", "please", "pls", "mandame", "enviame", "pasame")):
        object_value = _clean_request_object(normalized)
        return _ClaimDraft(
            text=f"A WhatsApp contact requested an action: {object_value}.",
            claim_type="request",
            subject="whatsapp_contact",
            predicate="requested_action",
            object_value=object_value,
            confidence=0.55,
        )

    return None


def _citation_for_evidence(evidence: EvidenceItem, source_item: SourceItem | None) -> CitationInput:
    external_id = source_item.source_item_id if source_item else f"source-item-{evidence.source_item_id}"
    source_id = source_item.source_id if source_item else "unknown"
    return CitationInput(
        source=f"whatsapp://source/{source_id}/item/{external_id}",
        locator=f"evidence:{evidence.id}",
        excerpt=(evidence.text or "")[:500],
    )


def _claim_idempotency_key(evidence: EvidenceItem, draft: _ClaimDraft) -> str:
    digest = hashlib.sha256(
        f"{draft.claim_type}\n{draft.subject}\n{draft.predicate}\n{draft.object_value}".encode("utf-8")
    ).hexdigest()[:16]
    return f"atlas-claim:evidence:{evidence.id}:{digest}"


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _clean_request_object(text: str) -> str:
    cleaned = re.sub(
        r"(?i)^(hey|hola|che|please|pls|can you|could you|would you)\s+",
        "",
        text,
    )
    return cleaned.strip(" .?!")
