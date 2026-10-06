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


class RescopeConflict(ValueError):
    """The target scope already holds a different confirmed value for the same triple."""


def _confirmed_twin(store, claim, target_scope: str):
    """The target's confirmed public claim for the same (tenant, subject, predicate), if any.

    A unique index allows one per scope, so a copy that must pass through `confirmed`
    cannot be created next to it.
    """
    if not (claim.subject and claim.predicate and claim.visibility == "public"):
        return None
    with store.connect() as conn:
        row = conn.execute(
            "SELECT id, object_value FROM claims WHERE status = 'confirmed' AND visibility = 'public' "
            "AND COALESCE(tenant_id, '') = COALESCE(?, '') AND subject = ? AND predicate = ? AND scope = ?",
            (claim.tenant_id, claim.subject, claim.predicate, target_scope),
        ).fetchone()
    return None if row is None else (int(row[0]), row[1])
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
    if "confirmed" in _PATH[old.status]:
        twin = _confirmed_twin(store, old, target_scope)
        if twin is not None:
            twin_id, twin_value = twin
            if (twin_value or "") != (old.object_value or ""):
                raise RescopeConflict(f"Claim {claim_id}: {target_scope} already confirms a different "
                                      f"value for this triple (claim {twin_id}).")
            store.mark_superseded(claim_id, twin_id, f"rescope {old.scope} -> {target_scope}: {reason} "
                                  "(same confirmed claim already there)",
                                  event_payload={"rescope": True, "from_scope": old.scope,
                                                 "to_scope": target_scope, "merged_into_existing": True})
            return twin_id
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
    try:
        for step in _PATH[old.status]:
            lifecycle.transition_claim(store, new.id, step, reason=f"rescope from #{claim_id}: {reason}",
                                       event_type="transition")
    except ValueError as exc:
        # e.g. a Dreaming-sourced claim cannot be re-confirmed without a source review:
        # retire the half-made copy so no live orphan is left, and keep the original.
        lifecycle.transition_claim(store, new.id, "archived", reason=f"failed rescope of #{claim_id}: {exc}",
                                   event_type="transition")
        raise RescopeConflict(f"Claim {claim_id} cannot be copied to {target_scope}: {exc}") from exc
    store.adopt_claim_history(new.id, claim_id)
    store.mark_superseded(claim_id, new.id, f"rescope {old.scope} -> {target_scope}: {reason}",
                          event_payload={"rescope": True, "from_scope": old.scope, "to_scope": target_scope})
    return new.id
