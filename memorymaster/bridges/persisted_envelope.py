"""Canonical sensitivity envelope for legacy claim/citation row transport."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from memorymaster.core.models import CitationInput
from memorymaster.core.security import (
    SensitiveMetadataError,
    sanitize_claim_input,
    sanitize_persisted_text,
    validate_persisted_metadata,
)


_CLAIM_CONTENT_FIELDS = frozenset(
    {"text", "normalized_text", "subject", "predicate", "object_value"}
)
_CITATION_CONTENT_FIELDS = frozenset({"excerpt"})


@dataclass(frozen=True, slots=True)
class SanitizedClaimEnvelope:
    row: dict[str, object]
    citations: tuple[dict[str, object], ...]
    findings: tuple[str, ...]


def persisted_claim_id(row: Mapping[str, object]) -> int:
    """Return a numeric claim id or fail without echoing an unsafe value."""
    value = row.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise SensitiveMetadataError("claim_id", ["invalid_identifier"])


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _citation_inputs(
    citations: Sequence[Mapping[str, object]],
) -> list[CitationInput]:
    return [
        CitationInput(
            source=str(citation.get("source") or ""),
            locator=_optional_text(citation.get("locator")),
            excerpt=_optional_text(citation.get("excerpt")),
        )
        for citation in citations
    ]


def _contains_binary(value: object) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, Mapping):
        return any(
            _contains_binary(key) or _contains_binary(nested)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_binary(nested) for nested in value)
    return False


def _metadata_fields(
    prefix: str,
    values: Mapping[str, object],
    excluded: frozenset[str],
) -> dict[str, object]:
    fields: dict[str, object] = {}
    for index, (field, value) in enumerate(values.items()):
        if field in excluded:
            continue
        validate_persisted_metadata({f"{prefix}_field_name": field})
        field_name = f"{prefix}_field_{index}"
        if _contains_binary(value):
            raise SensitiveMetadataError(field_name, ["binary_metadata"])
        fields[field_name] = value
    return fields


def _validate_envelope_metadata(
    row: Mapping[str, object], citations: Sequence[Mapping[str, object]]
) -> None:
    validate_persisted_metadata(_metadata_fields("claim", row, _CLAIM_CONTENT_FIELDS))
    for citation in citations:
        validate_persisted_metadata(
            _metadata_fields("citation", citation, _CITATION_CONTENT_FIELDS)
        )


def _sanitize_claim_content(
    row: Mapping[str, object], citations: list[CitationInput]
):
    return sanitize_claim_input(
        text=str(row.get("text") or ""),
        object_value=_optional_text(row.get("object_value")),
        citations=citations,
        subject=_optional_text(row.get("subject")),
        predicate=_optional_text(row.get("predicate")),
        idempotency_key=_optional_text(row.get("idempotency_key")),
        claim_type=_optional_text(row.get("claim_type")),
        scope=_optional_text(row.get("scope")),
        volatility=_optional_text(row.get("volatility")),
        source_agent=_optional_text(row.get("source_agent")),
        visibility=_optional_text(row.get("visibility")),
        holder=_optional_text(row.get("holder")),
        confidence=row.get("confidence"),
        event_time=_optional_text(row.get("event_time")),
        valid_from=_optional_text(row.get("valid_from")),
        valid_until=_optional_text(row.get("valid_until")),
        tenant_id=_optional_text(row.get("tenant_id")),
    )


def _has_redaction_marker(
    row: Mapping[str, object], citations: Sequence[Mapping[str, object]]
) -> bool:
    claim_values = (row.get(field) for field in _CLAIM_CONTENT_FIELDS)
    citation_values = (citation.get("excerpt") for citation in citations)
    return any(
        isinstance(value, str) and "[REDACTED:" in value
        for value in (*claim_values, *citation_values)
    )


def sanitize_claim_envelope(
    row: Mapping[str, object],
    citations: Sequence[Mapping[str, object]] = (),
) -> SanitizedClaimEnvelope:
    """Return sanitized copies or reject secret-shaped metadata fail-closed."""
    raw_row = dict(row)
    raw_citations = tuple(dict(citation) for citation in citations)
    _validate_envelope_metadata(raw_row, raw_citations)
    citation_inputs = _citation_inputs(raw_citations)
    sanitized = _sanitize_claim_content(raw_row, citation_inputs)
    normalized = _optional_text(raw_row.get("normalized_text"))
    normalized_findings: list[str] = []
    if normalized is not None:
        normalized, normalized_findings = sanitize_persisted_text(normalized)
    safe_row = {
        **raw_row,
        "text": sanitized.text,
        "subject": sanitized.subject,
        "predicate": sanitized.predicate,
        "object_value": sanitized.object_value,
        "normalized_text": normalized,
    }
    safe_citations = tuple(
        {
            **raw,
            "source": safe.source,
            "locator": safe.locator,
            "excerpt": safe.excerpt,
        }
        for raw, safe in zip(raw_citations, sanitized.citations)
    )
    findings_set = set(sanitized.findings + normalized_findings)
    if _has_redaction_marker(safe_row, safe_citations):
        findings_set.add("redaction_marker")
    findings = tuple(sorted(findings_set))
    return SanitizedClaimEnvelope(safe_row, safe_citations, findings)


def claim_envelope_is_safe(
    row: Mapping[str, object],
    citations: Sequence[Mapping[str, object]] = (),
) -> bool:
    """Return False for legacy rows that would be rejected or redacted."""
    try:
        return not sanitize_claim_envelope(row, citations).findings
    except (SensitiveMetadataError, ValueError, TypeError):
        return False
