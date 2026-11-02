"""The "authorized for this caller" predicates for claim reads (T-2070).

Extracted from ``service.py``, which re-exports both names so existing
imports (and the service's own call sites) keep working.
"""
from __future__ import annotations

from memorymaster.core.models import Claim
from memorymaster.core.security import is_sensitive_claim


def filter_agent_visibility(claims: list[Claim], requesting_agent: str | None) -> list[Claim]:
    """Drop other agents' PRIVATE claims for a per-agent visibility query.

    A claim is visible to ``requesting_agent`` when it is public OR was authored
    by that same agent. With no requesting agent the list is returned unchanged.
    Must be applied on EVERY retrieval path (legacy, hybrid, cache rehydrate) or
    a path-dependent cross-agent leak appears.
    """
    if not requesting_agent:
        return claims
    return [
        c
        for c in claims
        if getattr(c, "visibility", "public") == "public"
        or getattr(c, "source_agent", None) == requesting_agent
    ]


def authorize_claims(
    claims: list[Claim],
    *,
    requesting_agent: str | None = None,
    tenant_id: str | None = None,
    include_sensitive: bool = False,
) -> list[Claim]:
    """The one "authorized for this caller" predicate for claim list reads.

    Tenant boundary, per-agent visibility, then the sensitivity filter. Every
    list read that bypasses ``list_claims`` (store-level reads, reads by id)
    must go through this so the policy lives in one place (T-2070).
    """
    if tenant_id is not None:
        claims = [c for c in claims if getattr(c, "tenant_id", None) == tenant_id]
    claims = filter_agent_visibility(claims, requesting_agent)
    if not include_sensitive:
        claims = [c for c in claims if not is_sensitive_claim(c)]
    return claims
