"""Dense candidates for ``retrieve()``: the vector service proposes ids, the store decides.

``query_memory`` used to be lexical-only by default while the prompt hook already used
the EmbeddingGemma service (``dense_recall``), so an agent that asked got worse recall
than the one that was told. This is the same candidate source with the same cutoff
(``dense_recall.min_score``), rehydrated through the authoritative store: status,
scope, tenant, temporal validity, per-agent visibility and sensitivity are re-applied
to every id, so a stale or wrong vector can propose a claim but never expose one.

Returns ``(rows, reason)``. Empty ``rows`` means "answer lexically" and ``reason`` says
why; the caller reports it as the plan's ``containment_reason``. Policy, no silent gaps:

* the index holds confirmed claims only, so any wider status set stays lexical;
* ``allow_sensitive`` stays lexical (the lexical path owns that decision);
* dense and lexical rows are fused by reciprocal rank (``fuse_rows``). The hook replaces
  lexical with dense because it injects 2-6 claims unasked; an explicit query also needs the
  exact-identifier hits dense misses (measured 2026-11-03: "heavy-slot bulk gate" ranked its
  own claim 5th dense-only, 1st fused);
* zero authorized dense rows answers lexically rather than nothing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from memorymaster.core.claim_authorization import authorize_claims
from memorymaster.core.temporal_policy import claim_is_temporally_current
from memorymaster.recall import dense_recall
from memorymaster.recall.planner import RetrievalPlan
from memorymaster.recall.recall_fusion import rrf_fuse
from memorymaster.recall.retrieval import _freshness_score, pending_supersession_ids

if TYPE_CHECKING:
    from memorymaster.core.service import MemoryService

logger = logging.getLogger(__name__)

# Ask for more than the limit: the store drops unauthorized ids after the service answers.
_OVERFETCH = 3
_MAX_K = 100


def dense_rows(svc: MemoryService, plan: RetrievalPlan) -> tuple[list[dict[str, Any]], str | None]:
    if plan.statuses != ("confirmed",):
        return [], "dense recall indexes confirmed claims only; lexical search used"
    if plan.allow_sensitive:
        return [], "allow_sensitive requests use lexical search"
    if not plan.query_text.strip():
        return [], "empty query; lexical search used"

    requesting_agent = svc._effective_requesting_agent(plan.requesting_agent)
    if requesting_agent:
        from memorymaster.core.access_control import require_permission

        require_permission(requesting_agent, "query")
    scopes = svc._effective_scope_allowlist(list(plan.scope_allowlist) if plan.scope_allowlist else None)

    try:
        answer = dense_recall.search(plan.query_text, scopes, k=min(max(plan.limit * _OVERFETCH, dense_recall.FETCH_K), _MAX_K))
    except dense_recall.DenseCold:
        return [], "dense service is warming up (model cold); lexical search used"
    except dense_recall.DenseUnavailable as exc:
        return [], f"dense service unavailable ({exc}); lexical search used"

    threshold = dense_recall.min_score()
    scored = {cid: score for cid, score in answer.results if score >= threshold}
    if not scored:
        return [], f"no dense candidate reached cosine {threshold}; lexical search used"

    claims = svc.store.list_claims(
        ids=list(scored),
        status_in=list(plan.statuses),
        limit=len(scored),
        include_archived=False,
        include_citations=True,
        scope_allowlist=scopes,
        tenant_id=svc.tenant_id,
    )
    claims = authorize_claims(
        [claim for claim in claims if claim_is_temporally_current(claim)],
        requesting_agent=requesting_agent,
        include_sensitive=False,
    )
    if not claims:
        return [], "no dense candidate passed authorization; lexical search used"

    demoted = pending_supersession_ids(svc)
    claims.sort(key=lambda claim: (claim.id in demoted, -scored[claim.id]))
    rows = [
        {
            "claim": claim,
            "status": claim.status,
            "annotation": svc._annotation_for_claim(claim),
            "score": scored[claim.id],
            "lexical_score": 0.0,
            "freshness_score": _freshness_score(claim),
            "confidence_score": claim.confidence,
            "vector_score": scored[claim.id],
            "breakdown": {"source": "dense", "model": answer.model, "cosine": scored[claim.id]},
        }
        for claim in claims[: plan.limit]
    ]
    return rows, None


def fuse_rows(dense: list[dict[str, Any]], lexical: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Reciprocal-rank fusion of the dense and lexical rows; a claim in both keeps both signals."""
    by_id: dict[int, dict[str, Any]] = {}
    for row in lexical:
        by_id[row["claim"].id] = row
    for row in dense:
        merged = by_id.get(row["claim"].id)
        by_id[row["claim"].id] = (
            {**merged, "vector_score": row["vector_score"], "breakdown": {**row["breakdown"], "lexical": True}}
            if merged is not None
            else row
        )
    fused = rrf_fuse({
        "dense": [row["claim"].id for row in dense],
        "lexical": [row["claim"].id for row in lexical],
    })
    order = sorted(by_id, key=lambda cid: -fused[cid])
    return [{**by_id[cid], "score": fused[cid]} for cid in order[:limit]]
