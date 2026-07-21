"""Immutable contracts and validation for the native Dreaming pipeline."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from memorymaster.core.security import redact_text


PERSONAL_CLAIM_TYPES = frozenset({"preference", "profile", "constraint"})
DECISION_ACTIONS = frozenset({
    "add", "reinforce", "propose_supersede", "propose_stale", "propose_conflict", "ignore",
})
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\[^\r\n\t\"'<>|]{2,}")
_COMMIT_HASH = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DreamMessage:
    message_id: str
    role: str
    text: str
    timestamp: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CaptureEnvelope:
    provider: str
    session_hash: str
    scope: str
    captured_at: str
    last_activity_at: str
    messages: tuple[DreamMessage, ...]
    cursor_start: int
    cursor_end: int
    content_hash: str
    version: str = "dream.capture.v1"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["messages"] = [message.to_dict() for message in self.messages]
        return payload


@dataclass(frozen=True, slots=True)
class DreamCandidate:
    candidate_id: str
    text: str
    claim_type: str
    subject: str
    predicate: str
    object_value: str | None
    scope_class: str
    evidence_message_id: str
    evidence_quote: str
    confidence: float
    valid_from: str | None = None
    valid_until: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DreamDecision:
    candidate_id: str
    action: str
    rationale: str
    confidence: float
    target_claim_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    provider: str
    model: str
    http_status: int
    latency_ms: int
    input_tokens: int
    output_tokens: int
    structured_valid: bool


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    candidates: tuple[DreamCandidate, ...]
    usage: ProviderUsage


@dataclass(frozen=True, slots=True)
class ConsolidationResult:
    decisions: tuple[DreamDecision, ...]
    usage: ProviderUsage


def _clean_required(payload: dict[str, Any], key: str, *, minimum: int = 1) -> str:
    value = str(payload.get(key, "") or "").strip()
    if len(value) < minimum:
        raise ValueError(f"{key} is required")
    return value


def _candidate_id(capture_hash: str, index: int, text: str) -> str:
    material = f"{capture_hash}|{index}|{text.strip().lower()}"
    return "dc-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _finite_confidence(value: Any, *, default: float) -> float:
    confidence = float(default if value is None else value)
    if not math.isfinite(confidence):
        raise ValueError("confidence must be finite")
    return min(1.0, max(0.0, confidence))


def _project_specific_personal(candidate: DreamCandidate) -> bool:
    joined = " | ".join(filter(None, (candidate.text, candidate.subject, candidate.object_value)))
    markers = ("project:", "commit ", "branch ", ".py", ".ts", ".tsx", ".js", ".sql")
    return bool(_WINDOWS_PATH.search(joined) or _COMMIT_HASH.search(joined) or any(m in joined.lower() for m in markers))


def candidate_from_payload(
    payload: dict[str, Any], capture_hash: str, index: int, messages: Iterable[dict[str, Any]],
) -> DreamCandidate:
    text = _clean_required(payload, "text", minimum=10)
    evidence_id = _clean_required(payload, "evidence_message_id")
    evidence_quote = _clean_required(payload, "evidence_quote", minimum=3)
    by_id = {str(message.get("id") or message.get("message_id")): str(message.get("text", "")) for message in messages}
    if evidence_id not in by_id or evidence_quote not in by_id[evidence_id]:
        raise ValueError("evidence quote is not an exact substring of the sanitized message")
    scope_class = str(payload.get("scope_class", "project") or "project").strip().lower()
    if scope_class not in {"project", "personal"}:
        raise ValueError("scope_class must be project or personal")
    claim_type = _clean_required(payload, "claim_type").lower()
    subject = _clean_required(payload, "subject")[:200]
    predicate = _clean_required(payload, "predicate")[:200]
    object_value = (
        str(payload.get("object_value")).strip()[:1000]
        if payload.get("object_value") is not None
        else None
    )
    sensitive_fields = (text, subject, predicate, object_value or "", evidence_quote)
    if redact_text(" | ".join(sensitive_fields))[1]:
        raise ValueError("candidate contains sensitive material")
    candidate = DreamCandidate(
        candidate_id=_candidate_id(capture_hash, index, text),
        text=text[:1000],
        claim_type=claim_type,
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        scope_class=scope_class,
        evidence_message_id=evidence_id,
        evidence_quote=evidence_quote[:500],
        confidence=_finite_confidence(payload.get("confidence"), default=0.6),
        valid_from=(str(payload.get("valid_from")).strip() or None) if payload.get("valid_from") else None,
        valid_until=(str(payload.get("valid_until")).strip() or None) if payload.get("valid_until") else None,
    )
    if scope_class == "personal" and (claim_type not in PERSONAL_CLAIM_TYPES or _project_specific_personal(candidate)):
        raise ValueError("personal candidate is not an allowlisted stable personal claim")
    return candidate


def decision_from_payload(payload: dict[str, Any], valid_candidate_ids: set[str]) -> DreamDecision:
    candidate_id = _clean_required(payload, "candidate_id")
    action = _clean_required(payload, "action").lower()
    if candidate_id not in valid_candidate_ids:
        raise ValueError("decision references an unknown candidate")
    if action not in DECISION_ACTIONS:
        raise ValueError("unsupported consolidation action")
    target = payload.get("target_claim_id")
    target_id = int(target) if target not in (None, "") else None
    if action.startswith("propose_") and (target_id is None or target_id <= 0):
        raise ValueError("proposal decisions require target_claim_id")
    return DreamDecision(
        candidate_id=candidate_id,
        action=action,
        rationale=str(payload.get("rationale", "") or "")[:500],
        confidence=_finite_confidence(payload.get("confidence"), default=0.5),
        target_claim_id=target_id,
    )
