"""Move a claim to the scope it belongs to, leaving a supersession trail.

Scope is part of a claim's identity for recall, so a claim is never re-scoped
in place: the content (text, triple, citations, confidence, bitemporal fields,
lifecycle status) is copied into the target scope and the original is
superseded by the copy. Reversing a move is another move back.
"""
from __future__ import annotations

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput

_RETIRED = {"superseded", "archived"}
# Lifecycle path from a fresh candidate to each live status.
_PATH = {"candidate": (), "confirmed": ("confirmed",), "stale": ("confirmed", "stale"),
         "conflicted": ("conflicted",)}


def rescope_claim(store, claim_id: int, target_scope: str, *, reason: str) -> int:
    """Return the id of the copy in ``target_scope``; the original is superseded."""
    target_scope = target_scope.strip()
    if not target_scope:
        raise ValueError("target_scope is required.")
    old = store.get_claim(claim_id, include_citations=True)
    if old is None:
        raise ValueError(f"Claim {claim_id} does not exist.")
    if old.status in _RETIRED:
        raise ValueError(f"Claim {claim_id} is {old.status}; only live claims move.")
    if old.scope == target_scope:
        raise ValueError(f"Claim {claim_id} is already in {target_scope}.")
    citations = [CitationInput(source=c.source, locator=c.locator, excerpt=c.excerpt) for c in old.citations]
    if not citations:
        citations = [CitationInput(source=f"claim://{claim_id}", locator="rescope")]
    new = store.create_claim(
        text=old.text, citations=citations, claim_type=old.claim_type, subject=old.subject,
        predicate=old.predicate, object_value=old.object_value, scope=target_scope,
        volatility=old.volatility, confidence=old.confidence, tenant_id=old.tenant_id,
        event_time=old.event_time, valid_from=old.valid_from, valid_until=old.valid_until,
        source_agent=old.source_agent, visibility=old.visibility, holder=old.holder,
    )
    for step in _PATH[old.status]:
        lifecycle.transition_claim(store, new.id, step, reason=f"rescope from #{claim_id}: {reason}",
                                   event_type="transition")
    store.adopt_claim_history(new.id, claim_id)
    store.mark_superseded(claim_id, new.id, f"rescope {old.scope} -> {target_scope}: {reason}",
                          event_payload={"rescope": True, "from_scope": old.scope, "to_scope": target_scope})
    return new.id
