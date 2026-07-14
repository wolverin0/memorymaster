"""Canonical identity namespaces for claims.

All claim identities are local to the exact tenant and scope visible to the
caller. Non-public identities additionally include visibility and source
principal so hidden rows cannot become uniqueness oracles.
"""
from __future__ import annotations

from typing import Final, TypeVar


CLAIM_VISIBILITIES: Final[frozenset[str]] = frozenset(
    {"public", "private", "sensitive"}
)
_Row = TypeVar("_Row")


def normalize_claim_visibility(value: str | None) -> str:
    """Return a canonical visibility or reject an unsupported value."""
    normalized = str(value or "public").strip().lower()
    if normalized not in CLAIM_VISIBILITIES:
        allowed = ", ".join(sorted(CLAIM_VISIBILITIES))
        raise ValueError(f"Invalid claim visibility {value!r}; expected one of: {allowed}.")
    return normalized


def normalize_source_agent(value: str | None) -> str | None:
    """Collapse blank principal labels to ``None``."""
    return str(value or "").strip() or None


def normalize_claim_identity(
    visibility: str | None,
    source_agent: str | None,
    *,
    allow_sensitive: bool = True,
) -> tuple[str, str | None]:
    """Validate and normalize one claim's persisted identity context."""
    normalized_visibility = normalize_claim_visibility(visibility)
    normalized_source = normalize_source_agent(source_agent)
    if normalized_visibility == "sensitive" and not allow_sensitive:
        raise PermissionError("Sensitive claim writes are unavailable in team runtime mode.")
    if normalized_visibility != "public" and normalized_source is None:
        raise ValueError("Non-public claims require a non-blank source_agent principal.")
    return normalized_visibility, normalized_source


def identity_namespace_key(
    tenant_id: str | None,
    scope: str,
    visibility: str,
    source_agent: str | None,
) -> tuple[str | None, str, str, str | None]:
    """Return the in-memory key used by collision allocators."""
    principal = source_agent if visibility != "public" else None
    return tenant_id, scope, visibility, principal


def require_unambiguous_identity_row(
    rows: list[_Row],
    *,
    identifier: str,
) -> _Row | None:
    """Return the sole visible identity row or reject a missing scope choice."""
    if len(rows) > 1:
        raise ValueError(
            f"Ambiguous {identifier}; provide an exact claim scope."
        )
    return rows[0] if rows else None
