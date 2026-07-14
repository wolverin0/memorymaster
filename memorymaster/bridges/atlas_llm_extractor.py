"""LLM-driven typed-entity Atlas claim extraction from source evidence.

Replaces the deterministic keyword matcher in ``atlas_claim_extractor`` with an
LLM pass that reads evidence *bodies* (+ sender + date + provider) and emits
0..N typed life-knowledge claims (person/company/project/commitment/decision/
preference/fact/event/...), or **nothing** for newsletters / bot-notifications /
OTP / receipts / pure FYI.

The deterministic path in ``atlas_claim_extractor`` is preserved unchanged; the
CLI selects between them. This module reuses ``AtlasClaimExtractionResult`` so
the bridge/CLI contract is identical, extended with ``degraded`` + ``emitted``
counters.

Hardening vs the deterministic bug:
  * subjects are the *real entity*, never a bare source name ("Gmail"/"WhatsApp")
  * citations are provider-aware (``gmail://``/``outlook://``/...), not a
    hardcoded ``whatsapp://``
  * malformed/empty/erroring LLM output → skip + ``degraded``, never a junk claim
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass
from typing import Any

from memorymaster.bridges.atlas_claim_extractor import AtlasClaimExtractionResult
from memorymaster.bridges.evidence_policy import is_governed_evidence_eligible
from memorymaster.core.llm_provider import call_llm, parse_json_response
from memorymaster.core.models import CitationInput, Claim, EvidenceItem, SourceItem

logger = logging.getLogger(__name__)

__all__ = [
    "extract_atlas_claims_llm",
    "AtlasLlmExtractionResult",
    "ALLOWED_TYPES",
    "LLM_PROMPT_VERSION",
]

# Bump when the prompt changes so idempotency keys / caches invalidate.
LLM_PROMPT_VERSION = "atlas-llm-v1-2026-06-22"

# Permitted typed-claim ``type`` values. Anything else is dropped.
ALLOWED_TYPES: frozenset[str] = frozenset(
    {
        "person",
        "company",
        "project",
        "product",
        "topic",
        "decision",
        "commitment",
        "preference",
        "fact",
        "event",
    }
)

# Bare source/connector names that must never be a claim subject — these are the
# exact false positives observed on the live VM (subject-line wrappers, etc.).
_BARE_SOURCE_NAMES: frozenset[str] = frozenset(
    {
        "gmail",
        "whatsapp",
        "whatsapp live",
        "google drive",
        "outlook mail",
        "onedrive",
        "personaldashboard",
        "google calendar",
        "outlook calendar",
    }
)

# Generic placeholder subjects that carry no real entity — including the exact
# original-bug subject "whatsapp_contact". Any "<source>_contact" form is also
# rejected (see _validate_row), so the mm-d993 junk shape can never be ingested.
_GENERIC_PLACEHOLDER_SUBJECTS: frozenset[str] = frozenset(
    {
        "contact",
        "whatsapp_contact",
        "email_contact",
        "sender",
        "recipient",
        "unknown",
        "user",
        "someone",
        "n/a",
        "none",
    }
)

# Provider token (lowercased) -> citation URI scheme.
_PROVIDER_SCHEMES: dict[str, str] = {
    "gmail": "gmail",
    "google_mail": "gmail",
    "outlook": "outlook",
    "outlook_mail": "outlook",
    "gcal": "gcal",
    "google_calendar": "gcal",
    "outlook_calendar": "gcal",
    "gdrive": "gdrive",
    "google_drive": "gdrive",
    "onedrive": "gdrive",
    "whatsapp": "whatsapp",
    "whatsapp_live": "whatsapp",
}

_DEFAULT_CONFIDENCE = 0.6
_MAX_BODY_CHARS = 4000
_EXCERPT_CHARS = 500


@dataclass(frozen=True)
class AtlasLlmExtractionResult(AtlasClaimExtractionResult):
    """``AtlasClaimExtractionResult`` extended with LLM-pass counters.

    ``degraded`` — evidence items skipped because the LLM returned empty /
    malformed JSON or raised (no claim emitted for them).
    ``emitted`` — total valid typed-claim objects produced by the LLM and
    passing validation (== ``ingested`` unless ``dry_run``).
    """

    degraded: int = 0
    emitted: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "matched": self.matched,
            "ingested": self.ingested,
            "degraded": self.degraded,
            "emitted": self.emitted,
            "claims": [asdict(claim) for claim in self.claims],
        }


@dataclass(frozen=True)
class _TypedClaim:
    claim_type: str
    subject: str
    predicate: str
    object_value: str
    text: str
    confidence: float
    event_time: str | None


def extract_atlas_claims_llm(
    service,
    *,
    scope: str,
    limit: int = 200,
    model: str | None = None,
    dry_run: bool = False,
) -> AtlasLlmExtractionResult:
    """Extract typed life-knowledge claims from evidence via an LLM pass.

    For each evidence item: load its source item, prompt the LLM with the body
    (+ provider/sender/date), parse a strict JSON array of typed claims, validate
    each, and route valid ones through ``service.ingest`` (unless ``dry_run``).
    Empty/malformed/erroring LLM output skips the item and increments
    ``degraded`` — never a fallback junk claim.
    """
    evidence_items = service.list_evidence_items(limit=limit)
    scanned = 0
    matched = 0
    ingested = 0
    degraded = 0
    emitted = 0
    claims: list[Claim] = []

    for evidence in evidence_items:
        scanned += 1
        if not is_governed_evidence_eligible(evidence):
            continue
        source_item = service.get_source_item_by_id(evidence.source_item_id)
        typed_claims = _extract_for_evidence(evidence, source_item, model=model)
        if typed_claims is None:
            degraded += 1
            continue
        if typed_claims:
            matched += 1
        for typed in typed_claims:
            emitted += 1
            if dry_run:
                continue
            claim = _ingest_typed_claim(service, evidence, source_item, typed, scope=scope)
            ingested += 1
            claims.append(claim)

    return AtlasLlmExtractionResult(
        scanned=scanned,
        matched=matched,
        ingested=ingested,
        degraded=degraded,
        emitted=emitted,
        claims=claims,
    )


def _extract_for_evidence(
    evidence: EvidenceItem,
    source_item: SourceItem | None,
    *,
    model: str | None,
) -> list[_TypedClaim] | None:
    """Return validated typed claims, or ``None`` on a degraded (skipped) item."""
    body = (evidence.text or "").strip()
    if not body:
        return None
    prompt = _build_prompt(evidence, source_item)
    snippet = body if len(body) <= _MAX_BODY_CHARS else body[:_MAX_BODY_CHARS]
    raw = _call_llm_safe(prompt, snippet, model=model)
    if raw is None:
        return None
    rows = _parse_rows(raw)
    if rows is None:
        return None
    typed: list[_TypedClaim] = []
    for row in rows:
        candidate = _validate_row(row)
        if candidate is not None:
            typed.append(candidate)
    return typed


def _call_llm_safe(prompt: str, text: str, *, model: str | None) -> str | None:
    """Call the LLM, temporarily pinning ``model`` if given. ``None`` on failure."""
    import os

    previous = os.environ.get("MEMORYMASTER_LLM_MODEL")
    if model:
        os.environ["MEMORYMASTER_LLM_MODEL"] = model
    try:
        raw = call_llm(prompt, text)
    except Exception as exc:  # noqa: BLE001 — defensive, never re-raise
        logger.warning("atlas-llm: call_llm failed: %s", exc)
        return None
    finally:
        if model:
            if previous is None:
                os.environ.pop("MEMORYMASTER_LLM_MODEL", None)
            else:
                os.environ["MEMORYMASTER_LLM_MODEL"] = previous
    if not raw or not raw.strip():
        return None
    return raw


def _parse_rows(raw: str) -> list[dict] | None:
    """Parse a JSON array of claim objects. ``None`` on malformed output."""
    try:
        parsed = parse_json_response(raw)
    except Exception:  # noqa: BLE001 — parse_json_response is already defensive
        logger.warning("atlas-llm: parse_json_response raised unexpectedly")
        return None
    if not isinstance(parsed, list):
        return None
    return [row for row in parsed if isinstance(row, dict)]


def _validate_row(row: dict) -> _TypedClaim | None:
    """Validate one LLM claim object into a ``_TypedClaim``, or drop it."""
    raw_type = str(row.get("type", "")).strip().lower()
    if raw_type not in ALLOWED_TYPES:
        return None
    subject = str(row.get("subject", "")).strip()
    subject_l = subject.lower()
    if (
        not subject
        or subject_l in _BARE_SOURCE_NAMES
        or subject_l in _GENERIC_PLACEHOLDER_SUBJECTS
        or subject_l.endswith("_contact")
    ):
        return None
    text = str(row.get("text", "")).strip()
    if not text:
        return None
    return _TypedClaim(
        claim_type=raw_type,
        subject=subject,
        predicate=str(row.get("predicate", "")).strip(),
        object_value=str(row.get("object", "")).strip(),
        text=text,
        confidence=_clamp_confidence(row.get("confidence")),
        event_time=_normalize_event_time(row.get("event_time")),
    )


def _clamp_confidence(value: Any) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, conf))


def _normalize_event_time(value: Any) -> str | None:
    """Only a real ISO date(-time) survives. Despite the prompt, the LLM emits
    junk like 'later', '18:00', '2028', or bare time ranges — a bogus
    event_time poisons every ``date(event_time)`` query downstream (junk sorts
    before real dates because ``date()`` yields NULL), so junk is DROPPED (the
    claim ingests without an event_time) rather than stored."""
    if value is None:
        return None
    import re as _re
    from datetime import datetime as _dt

    text = str(value).strip()
    if not _re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        _dt.fromisoformat(text[:10])  # validates the date part (e.g. no month 13)
    except ValueError:
        return None
    return text


def _ingest_typed_claim(
    service,
    evidence: EvidenceItem,
    source_item: SourceItem | None,
    typed: _TypedClaim,
    *,
    scope: str,
) -> Claim:
    return service.ingest(
        text=typed.text,
        citations=[_citation_for_evidence(evidence, source_item)],
        idempotency_key=_claim_idempotency_key(evidence, typed),
        claim_type=typed.claim_type,
        subject=typed.subject,
        predicate=typed.predicate,
        object_value=typed.object_value,
        scope=scope,
        confidence=typed.confidence,
        event_time=typed.event_time,
        volatility="medium",
        source_agent="atlas-llm-extractor",
    )


def _claim_idempotency_key(evidence: EvidenceItem, typed: _TypedClaim) -> str:
    digest = hashlib.sha256(
        f"{typed.claim_type}\n{typed.subject}\n{typed.predicate}\n{typed.object_value}".encode("utf-8")
    ).hexdigest()[:16]
    return f"atlas-llm:evidence:{evidence.id}:{digest}"


def _citation_for_evidence(evidence: EvidenceItem, source_item: SourceItem | None) -> CitationInput:
    scheme = _citation_scheme(evidence, source_item)
    external_id = source_item.source_item_id if source_item else f"source-item-{evidence.source_item_id}"
    source_id = source_item.source_id if source_item else "unknown"
    return CitationInput(
        source=f"{scheme}://source/{source_id}/item/{external_id}",
        locator=f"evidence:{evidence.id}",
        excerpt=(evidence.text or "")[:_EXCERPT_CHARS],
    )


def _citation_scheme(evidence: EvidenceItem, source_item: SourceItem | None) -> str:
    provider = (evidence.provider or "").strip().lower()
    if not provider and source_item is not None:
        provider = (getattr(source_item, "item_type", "") or "").strip().lower()
    return _PROVIDER_SCHEMES.get(provider, "atlas")


# -- Prompt -----------------------------------------------------------------


def _build_prompt(evidence: EvidenceItem, source_item: SourceItem | None) -> str:
    provider = (evidence.provider or "unknown").strip() or "unknown"
    sender = (getattr(source_item, "sender_name", None) or "unknown") if source_item else "unknown"
    occurred = (getattr(source_item, "occurred_at", None) or "unknown") if source_item else "unknown"
    header = (
        f"ITEM METADATA — provider: {provider} | sender: {sender} | date: {occurred}\n"
        "Read the body that follows this prompt and extract typed life-knowledge.\n"
    )
    return _PROMPT_BODY + "\n" + header


_PROMPT_BODY = f"""You extract durable, typed life-knowledge from a single message/document so a future personal AI agent can act on it. Prompt version: {LLM_PROMPT_VERSION}.

Emit ONLY facts that are DURABLE and USEFUL knowledge about the USER's world — real people, companies, projects, products, decisions, commitments/deadlines, preferences, or notable events. The `subject` MUST be the REAL named entity (a person/company/project), NEVER the source name (Gmail, WhatsApp, Outlook, Google Drive, etc.).

Return STRICT JSON ARRAY of objects, no prose, no code fence. Return [] (empty array) when the item is NOT durable life-knowledge, including:
- newsletters, marketing, promotions
- automated/bot notifications (GitHub, Vercel, PostHog, CI, system alerts)
- OTP / 2FA / verification codes
- receipts or confirmations with no future obligation
- pure FYI with nothing actionable or memorable

ALLOWED type values (exactly one per object): person, company, project, product, topic, decision, commitment, preference, fact, event.

Each object schema (use EXACT field names):
  {{"type":"...","subject":"<real entity name>","predicate":"<relation>","object":"<value>","text":"<one self-contained sentence a future agent can act on>","confidence":0.0-1.0,"event_time":"<ISO-8601 if a date/deadline is present, else omit>","relationship_to_user":"<optional, e.g. 'client','colleague','vendor'>"}}

Rules:
- `text` is one self-contained sentence — no pronouns referring outside it.
- Set `event_time` (ISO-8601) when there is a date/deadline, especially for commitments and events.
- Be conservative: when in doubt whether something is durable, return [].

EXAMPLE 1 (commitment with deadline)
Input body: "Hola Pablo, te confirmo que te paso el comprobante del pago el viernes 26."
Output: [{{"type":"commitment","subject":"Pablo","predicate":"will_send","object":"payment receipt","text":"A contact named Pablo committed to sending the payment receipt on Friday the 26th.","confidence":0.8,"event_time":"2026-06-26","relationship_to_user":"contact"}}]

EXAMPLE 2 (person fact)
Input body: "Just so you know, María González is now the new CTO at Acme Corp."
Output: [{{"type":"person","subject":"María González","predicate":"role_at","object":"CTO at Acme Corp","text":"María González is now the CTO at Acme Corp.","confidence":0.85,"relationship_to_user":"professional contact"}}]

EXAMPLE 3 (newsletter / nothing)
Input body: "🎉 Black Friday is here! 50% off all plans. Click to upgrade now and don't miss out!"
Output: []
""".strip()
