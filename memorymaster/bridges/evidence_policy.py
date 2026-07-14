"""Authenticity policy for media-derived evidence.

Synthetic evidence may be useful in tests and demonstrations, but it must be
both conspicuously enabled and permanently excluded from governed knowledge
and action paths.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

_NONPRODUCTION_MODES = frozenset({"development", "test"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SYNTHETIC_PROVIDER_TOKENS = frozenset({"mock", "synthetic", "placeholder", "fake", "fixture"})


def is_synthetic_provider(provider: str | None) -> bool:
    """Return whether a provider identity explicitly denotes fabricated data."""
    normalized = (provider or "").strip().lower().replace("_", "-")
    if not normalized:
        return False
    tokens = frozenset(part for part in normalized.split("-") if part)
    return bool(tokens & _SYNTHETIC_PROVIDER_TOKENS)


def synthetic_media_configuration_error(
    provider: str | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return an actionable error when a synthetic provider is not allowed."""
    if not is_synthetic_provider(provider):
        return None
    env = os.environ if environ is None else environ
    mode = env.get("MEMORYMASTER_MEDIA_MODE", "production").strip().lower()
    allowed = env.get("MEMORYMASTER_ALLOW_SYNTHETIC_MEDIA", "").strip().lower()
    if mode in _NONPRODUCTION_MODES and allowed in _TRUE_VALUES:
        return None
    return (
        f"Synthetic media provider '{provider}' is disabled. Production requires an explicitly "
        "configured real provider. For conspicuous test/development use only, set "
        "MEMORYMASTER_MEDIA_MODE=test (or development) and "
        "MEMORYMASTER_ALLOW_SYNTHETIC_MEDIA=1."
    )


def is_governed_evidence_eligible(evidence: Any) -> bool:
    """Exclude fabricated evidence from claims, actions, citations, and export."""
    return not is_synthetic_provider(getattr(evidence, "provider", None))
