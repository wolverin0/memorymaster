from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CLAIM_STATUSES = (
    "candidate",
    "confirmed",
    "stale",
    "superseded",
    "conflicted",
    "archived",
)

VOLATILITY_LEVELS = ("low", "medium", "high")

EVENT_TYPES = (
    "ingest",
    "extractor",
    "validator",
    "deterministic_validator",
    "decay",
    "compactor",
    "compaction_run",
    "dedup",
    "dedup_run",
    "supersession",
    "confidence",
    "policy_decision",
    "pin",
    "unpin",
    "audit",
    "sync",
    "system",
    "transition",
    "staleness",
)

STATUS_TRANSITION_EVENT_TYPES = (
    "transition",
    "validator",
    "deterministic_validator",
    "decay",
    "compactor",
    "dedup",
    "supersession",
    "staleness",
)


def validate_event_type(event_type: str) -> str:
    if not isinstance(event_type, str):
        raise ValueError("event_type must be a string.")
    normalized = event_type.strip()
    if not normalized:
        raise ValueError("event_type must be a non-empty string.")
    if normalized not in EVENT_TYPES:
        allowed = ", ".join(EVENT_TYPES)
        raise ValueError(f"Invalid event_type '{normalized}'. Allowed event types: {allowed}.")
    return normalized


def validate_transition_event_type(event_type: str) -> str:
    normalized = validate_event_type(event_type)
    if normalized not in STATUS_TRANSITION_EVENT_TYPES:
        allowed = ", ".join(STATUS_TRANSITION_EVENT_TYPES)
        raise ValueError(
            f"Invalid transition event_type '{normalized}'. Allowed transition event types: {allowed}."
        )
    return normalized


def _ensure_payload_dict(event_type: str, payload: dict[str, object] | None) -> dict[str, object]:
    if payload is None:
        raise ValueError(f"event_type '{event_type}' requires payload object.")
    if not isinstance(payload, dict):
        raise ValueError(f"event_type '{event_type}' payload must be a JSON object.")
    for key in payload:
        if not isinstance(key, str):
            raise ValueError(f"event_type '{event_type}' payload keys must be strings.")
    return payload


def _require_keys(event_type: str, payload: dict[str, object], required_keys: tuple[str, ...]) -> None:
    missing = [key for key in required_keys if key not in payload]
    if missing:
        raise ValueError(
            f"event_type '{event_type}' payload missing required keys: {', '.join(missing)}."
        )


def _as_int(event_type: str, payload: dict[str, object], key: str, minimum: int | None = None) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise ValueError(f"event_type '{event_type}' payload key '{key}' must be int.")
    if minimum is not None and value < minimum:
        raise ValueError(f"event_type '{event_type}' payload key '{key}' must be >= {minimum}.")
    return value


def _as_number(event_type: str, payload: dict[str, object], key: str, minimum: float | None = None, maximum: float | None = None) -> float:
    value = payload.get(key)
    if not isinstance(value, (int, float)):
        raise ValueError(f"event_type '{event_type}' payload key '{key}' must be number.")
    numeric = float(value)
    if minimum is not None and numeric < minimum:
        raise ValueError(f"event_type '{event_type}' payload key '{key}' must be >= {minimum}.")
    if maximum is not None and numeric > maximum:
        raise ValueError(f"event_type '{event_type}' payload key '{key}' must be <= {maximum}.")
    return numeric


def _as_bool(event_type: str, payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"event_type '{event_type}' payload key '{key}' must be bool.")
    return value


def _as_str(event_type: str, payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"event_type '{event_type}' payload key '{key}' must be non-empty string.")
    return value


def _is_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _validate_extractor_payload(p: dict[str, object]) -> dict[str, object]:
    """Validate extractor event payload."""
    _require_keys("extractor", p, ("claim_type", "subject", "predicate", "object_value"))
    return p


def _validate_ingest_payload(p: dict[str, object]) -> dict[str, object]:
    """Validate ingest event payload."""
    _require_keys("ingest", p, ("citation_count",))
    _as_int("ingest", p, "citation_count", minimum=1)
    return p


def _validate_compaction_run_payload(p: dict[str, object]) -> dict[str, object]:
    """Validate compaction_run event payload."""
    _require_keys("compaction_run", p, ("retain_days", "event_retain_days", "archived_claims", "deleted_events", "artifacts"))
    _as_int("compaction_run", p, "retain_days", minimum=0)
    _as_int("compaction_run", p, "event_retain_days", minimum=0)
    _as_int("compaction_run", p, "archived_claims", minimum=0)
    _as_int("compaction_run", p, "deleted_events", minimum=0)
    artifacts = p.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("event_type 'compaction_run' payload key 'artifacts' must be object.")
    for key in ("summary_graph", "traceability"):
        value = artifacts.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"event_type 'compaction_run' artifacts.{key} must be non-empty string.")
    return p


def _validate_validator_payload(p: dict[str, object] | None, details: str | None) -> dict[str, object] | None:
    """Validate validator event payload."""
    if p is None:
        if str(details or "") in {"validation_pending_more_evidence", "revalidation_passed"}:
            raise ValueError("event_type 'validator' requires payload for validation details.")
        return None
    p = _ensure_payload_dict("validator", p)
    _require_keys("validator", p, ("score", "citation_count"))
    _as_number("validator", p, "score", minimum=0.0, maximum=1.0)
    _as_int("validator", p, "citation_count", minimum=0)
    if "revalidation" in p:
        _as_bool("validator", p, "revalidation")
    return p


def _validate_deterministic_validator_payload(p: dict[str, object] | None) -> dict[str, object] | None:
    """Validate deterministic_validator event payload."""
    if p is None:
        return None
    p = _ensure_payload_dict("deterministic_validator", p)
    for key, value in p.items():
        if not _is_json_scalar(value):
            raise ValueError(
                f"event_type 'deterministic_validator' payload key '{key}' must be JSON scalar."
            )
    return p


def _validate_policy_decision_payload(p: dict[str, object]) -> dict[str, object]:
    """Validate policy_decision event payload."""
    if not p:
        raise ValueError("event_type 'policy_decision' payload cannot be empty.")
    return p


def _validate_audit_payload(p: dict[str, object] | None, details: str | None) -> dict[str, object] | None:
    """Validate audit event payload."""
    if p is None:
        return None
    p = _ensure_payload_dict("audit", p)
    if str(details or "").startswith("triage_"):
        _as_str("audit", p, "source")
    return p


def validate_event_payload(
    event_type: str,
    payload: dict[str, object] | None,
    *,
    details: str | None = None,
) -> dict[str, object] | None:
    normalized = validate_event_type(event_type)

    if normalized == "extractor":
        p = _ensure_payload_dict(normalized, payload)
        return _validate_extractor_payload(p)

    if normalized == "ingest":
        p = _ensure_payload_dict(normalized, payload)
        return _validate_ingest_payload(p)

    if normalized == "compaction_run":
        p = _ensure_payload_dict(normalized, payload)
        return _validate_compaction_run_payload(p)

    if normalized == "validator":
        return _validate_validator_payload(payload, details)

    if normalized == "deterministic_validator":
        return _validate_deterministic_validator_payload(payload)

    if normalized == "policy_decision":
        p = _ensure_payload_dict(normalized, payload)
        return _validate_policy_decision_payload(p)

    if normalized == "audit":
        return _validate_audit_payload(payload, details)

    # Other event types allow null/any-object payload.
    if payload is None:
        return None
    return _ensure_payload_dict(normalized, payload)


@dataclass(slots=True)
class CitationInput:
    source: str
    locator: str | None = None
    excerpt: str | None = None


@dataclass(slots=True)
class Citation:
    id: int
    claim_id: int
    source: str
    locator: str | None
    excerpt: str | None
    created_at: str


@dataclass(slots=True)
class Event:
    id: int
    claim_id: int | None
    event_type: str
    from_status: str | None
    to_status: str | None
    details: str | None
    payload_json: str | None
    created_at: str


CLAIM_LINK_TYPES = (
    # Core lifecycle types (original v2.0)
    "relates_to",
    "supersedes",
    "derived_from",
    "contradicts",
    "supports",
    # Domain-specific relationship types (GBrain-inspired, v3.3)
    "implements",       # claim A describes an implementation of claim B
    "configures",       # claim A configures/parametrizes claim B
    "depends_on",       # claim A requires claim B to function
    "deployed_on",      # claim A is deployed on infrastructure described by claim B
    "owned_by",         # claim A is owned/maintained by entity in claim B
    "tested_by",        # claim A is validated by test described in claim B
    "documents",        # claim A documents behavior of claim B
    "blocks",           # claim A blocks progress on claim B
    "enables",          # claim A enables/unlocks claim B
)


@dataclass(slots=True)
class ClaimLink:
    id: int
    source_id: int
    target_id: int
    link_type: str
    created_at: str


@dataclass(slots=True)
class Claim:
    id: int
    text: str
    idempotency_key: str | None
    normalized_text: str | None
    claim_type: str | None
    subject: str | None
    predicate: str | None
    object_value: str | None
    scope: str
    volatility: str
    status: str
    confidence: float
    pinned: bool
    supersedes_claim_id: int | None
    replaced_by_claim_id: int | None
    created_at: str
    updated_at: str
    last_validated_at: str | None
    archived_at: str | None
    human_id: str | None = None
    tenant_id: str | None = None
    tier: str = "working"
    access_count: int = 0
    last_accessed: str | None = None
    event_time: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    source_agent: str | None = None
    visibility: str = "public"
    version: int = 1
    wiki_article: str | None = None
    citations: list[Citation] = field(default_factory=list)
