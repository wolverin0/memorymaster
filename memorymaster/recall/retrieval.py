from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from memorymaster.core.config import get_config
from memorymaster.core.models import Claim
from memorymaster.recall.recall_fusion import RRF_K_DEFAULT, rrf_fuse

RETRIEVAL_MODES = ("legacy", "hybrid")

VectorSearchHook = Callable[[str, list[Claim]], Mapping[int, float]]

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "he",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "that",
    "the",
    "to",
    "was",
    "were",
    "will",
    "with",
}


@dataclass(slots=True)
class RankedClaim:
    claim: Claim
    score: float
    lexical_score: float
    freshness_score: float
    confidence_score: float
    vector_score: float
    breakdown: dict | None = None


@dataclass(slots=True)
class _ScoreParts:
    """Decomposed score: query-relevance vs. metadata boosts.

    ``relevance`` is the query-match signal (lexical + vector); ``boosts`` is
    everything else (confidence, freshness, tier, pinned). Keeping them
    separate lets the floor-ratio gate suppress boosts on weak matches and
    lets ``--explain`` show per-stage attribution.
    """
    relevance: float
    boosts: float
    weights: tuple[float, float, float, float]
    boost_terms: dict[str, float]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for token in _TOKEN_RE.findall(value.lower()):
        if len(token) < 3:
            continue
        if token in _STOPWORDS:
            continue
        tokens.add(token)
    return tokens


def _lexical_score(query_text: str, claim: Claim) -> float:
    q = query_text.strip().lower()
    if not q:
        return 0.0
    body = " ".join(x for x in [claim.text, claim.normalized_text or "", claim.subject or "", claim.object_value or ""] if x)
    body_lower = body.lower()

    q_tokens = _tokens(q)
    c_tokens = _tokens(body_lower)
    if not q_tokens or not c_tokens:
        return 0.0

    overlap = len(q_tokens & c_tokens)
    token_recall = overlap / max(1, len(q_tokens))
    token_precision = overlap / max(1, len(c_tokens))
    contains_phrase = 1.0 if q in body_lower else 0.0
    prefix_bonus = 1.0 if any(tok.startswith(q) for tok in c_tokens) and len(q) >= 3 else 0.0

    cfg = get_config()
    w_recall, w_precision, w_phrase, w_prefix = cfg.lexical_weights
    score = (w_recall * token_recall) + (w_precision * token_precision) + (w_phrase * contains_phrase) + (w_prefix * prefix_bonus)
    return max(0.0, min(1.0, score))


def _freshness_score(claim: Claim) -> float:
    anchor = _parse_iso(claim.last_validated_at) or _parse_iso(claim.updated_at) or _parse_iso(claim.created_at)
    if anchor is None:
        return 0.5
    now = datetime.now(timezone.utc)
    age_hours = max(0.0, (now - anchor).total_seconds() / 3600.0)
    cfg = get_config()
    half_life_hours = cfg.freshness_half_life_hours
    half_life = half_life_hours.get(claim.volatility, half_life_hours["medium"])
    return max(0.0, min(1.0, math.exp(-age_hours / max(1.0, half_life))))


_TIER_BONUS = {"core": 0.15, "working": 0.0, "peripheral": -0.10}


def _tier_bonus(claim: Claim) -> float:
    """Return a score adjustment based on the claim's memory tier."""
    tier = getattr(claim, "tier", "working") or "working"
    return _TIER_BONUS.get(tier, 0.0)


def rank_claims(
    query_text: str,
    claims: list[Claim],
    *,
    mode: str = "legacy",
    limit: int = 20,
    vector_hook: VectorSearchHook | None = None,
    semantic_vectors: bool = False,
    query_type: str | None = None,
) -> list[Claim]:
    return [
        row.claim
        for row in rank_claim_rows(
            query_text,
            claims,
            mode=mode,
            limit=limit,
            vector_hook=vector_hook,
            semantic_vectors=semantic_vectors,
            query_type=query_type,
        )
    ]


def _compute_score_parts(
    claim: Claim,
    lexical: float,
    confidence: float,
    freshness: float,
    vector: float,
    vector_enabled: bool,
    query_type: str | None = None,
) -> _ScoreParts:
    """Decompose a claim's score into query-relevance and metadata boosts.

    ``relevance`` = ``w_l*lexical (+ w_v*vector)`` — the part that measures
    "does this match the query". ``boosts`` = ``w_c*confidence + w_f*freshness
    + tier + pinned`` — the part that does not. The floor-ratio gate decides
    (in ``rank_claim_rows``) whether ``boosts`` apply, based on relevance
    relative to the top match.

    When ``query_type`` matches a configured per-type profile
    (``cfg.retrieval_profile(query_type)``), the profile's 4-tuple replaces
    ``cfg.retrieval_weights`` for vector-enabled paths.
    """
    cfg = get_config()
    if vector_enabled:
        profile = cfg.retrieval_profile(query_type) if query_type else None
        w_l, w_c, w_f, w_v = profile if profile is not None else cfg.retrieval_weights
        relevance = (w_l * lexical) + (w_v * vector)
    else:
        w_l, w_c, w_f = cfg.retrieval_weights_no_vector
        w_v = 0.0
        relevance = w_l * lexical
    conf_term = w_c * confidence
    fresh_term = w_f * freshness
    pinned_term = cfg.pinned_bonus if claim.pinned else 0.0
    tier_term = _tier_bonus(claim)
    boosts = conf_term + fresh_term + pinned_term + tier_term
    return _ScoreParts(
        relevance=relevance,
        boosts=boosts,
        weights=(w_l, w_c, w_f, w_v),
        boost_terms={
            "confidence": conf_term,
            "freshness": fresh_term,
            "pinned": pinned_term,
            "tier": tier_term,
        },
    )


def _assemble_breakdown(
    *,
    parts: _ScoreParts,
    lexical: float,
    confidence: float,
    freshness: float,
    vector: float,
    floor: float,
    gated: bool,
    final: float,
    graph: float | None = None,
    w_graph: float = 0.0,
) -> dict:
    """Build the per-claim score breakdown (observability only).

    Carries BOTH the legacy keys (``relevance``, ``boosts_total``,
    ``boosts_applied``, ``boost_terms``, ``weights``, ``floor``, ``final`` —
    relied on by existing explain/cache/qrels tests) AND the explicit
    component view the Recall Pattern Analyzer surfaces: the raw per-signal
    scores, the named weights actually applied, the weighted contributions,
    the relevance/boost subtotals, ``floor_gated``, and ``final_score``.

    ``graph`` (roadmap 12.2) is the optional distance-weighted graph signal
    ``1/(1+hops)`` carried on rows produced by the context_hook graph stream.
    It is keyed in only when a caller passes a non-None value — the default
    ``None`` leaves the breakdown byte-identical for every caller that does
    not compute a graph score (i.e. the entire RankedClaim ranker today), so
    the disabled-mode guarantee holds.

    No ranking math happens here — every value is derived from quantities the
    ranker already computed for this claim.
    """
    w_l, w_c, w_f, w_v = parts.weights
    boost_terms = parts.boost_terms
    breakdown = {
        # --- legacy keys (do not rename: explain/cache/qrels tests depend) ---
        "relevance": parts.relevance,
        "boosts_total": parts.boosts,
        "boosts_applied": not gated,
        "boost_terms": boost_terms,
        "weights": parts.weights,
        "floor": floor,
        "final": final,
        # --- explainability keys (Recall Pattern Analyzer) ---
        # Raw signals (pre-weight), each in [0, 1].
        "components": {
            "lexical": lexical,
            "confidence": confidence,
            "freshness": freshness,
            "vector": vector,
        },
        # Weighted contributions actually folded into the score.
        "contributions": {
            "lexical": w_l * lexical,
            "vector": w_v * vector,
            "confidence": boost_terms.get("confidence", 0.0),
            "freshness": boost_terms.get("freshness", 0.0),
            "tier_bonus": boost_terms.get("tier", 0.0),
            "pinned_bonus": boost_terms.get("pinned", 0.0),
        },
        # Named weights so callers don't index a bare 4-tuple.
        "weights_applied": {
            "lexical": w_l,
            "confidence": w_c,
            "freshness": w_f,
            "vector": w_v,
        },
        "tier_bonus": boost_terms.get("tier", 0.0),
        "pinned_bonus": boost_terms.get("pinned", 0.0),
        "relevance_subtotal": parts.relevance,
        "boosts_subtotal": 0.0 if gated else parts.boosts,
        "final_score": final,
        "floor_gated": gated,
    }
    # Optional graph component (roadmap 12.2). Only surfaced when a caller
    # actually carries a graph score — keeps the breakdown byte-identical for
    # the RankedClaim ranker, which never computes one.
    if graph is not None:
        breakdown["components"]["graph"] = graph
        breakdown["contributions"]["graph"] = w_graph * graph
        breakdown["weights_applied"]["graph"] = w_graph
    return breakdown


def _compute_claim_score(
    claim: Claim,
    lexical: float,
    confidence: float,
    freshness: float,
    vector: float,
    vector_enabled: bool,
    semantic_vectors: bool,
    query_type: str | None = None,
) -> float:
    """Backward-compatible total score (relevance + all boosts, no floor gate)."""
    parts = _compute_score_parts(
        claim, lexical, confidence, freshness, vector, vector_enabled, query_type
    )
    return parts.relevance + parts.boosts


def _source_session_key(row: RankedClaim) -> str:
    claim = row.claim
    if claim.source_agent:
        return claim.source_agent
    if claim.citations:
        source = claim.citations[0].source
        if source:
            return source
    if claim.subject:
        return claim.subject
    return f"claim:{claim.id}"


def apply_session_diversity_cap(ranked: list[RankedClaim], cap: int) -> list[RankedClaim]:
    if cap <= 0:
        return ranked
    counts: dict[str, int] = {}
    result: list[RankedClaim] = []
    for row in ranked:
        source_session = _source_session_key(row)
        count = counts.get(source_session, 0)
        if count >= cap:
            continue
        counts[source_session] = count + 1
        result.append(row)
    return result


def _component_rankings(rows: list[RankedClaim]) -> dict[str, list[int]]:
    original_positions = {row.claim.id: index for index, row in enumerate(rows)}

    def ranked_ids(score_attr: str) -> list[int]:
        ordered = sorted(
            rows,
            key=lambda row: (
                -float(getattr(row, score_attr)),
                original_positions[row.claim.id],
            ),
        )
        return [row.claim.id for row in ordered]

    return {
        "lexical": ranked_ids("lexical_score"),
        "vector": ranked_ids("vector_score"),
        "confidence": ranked_ids("confidence_score"),
        "freshness": ranked_ids("freshness_score"),
    }


def component_rankings(rows: list[RankedClaim]) -> dict[str, list[int]]:
    """Public view of per-component claim rankings (lexical/vector/confidence/
    freshness): for each signal, the claim ids ordered best-first. Pure
    observability — does not affect the ranked order in ``rows``.
    """
    return _component_rankings(rows)


def apply_rrf_tiebreaker(
    ranked: list[RankedClaim],
    *,
    threshold: float = 0.01,
    k: int = RRF_K_DEFAULT,
    enabled: bool = True,
) -> list[RankedClaim]:
    if not enabled or len(ranked) < 2:
        return ranked

    threshold = max(0.0, threshold)
    threshold_epsilon = 1e-12
    head_count = min(10, len(ranked))
    result = list(ranked)
    index = 0

    while index < head_count:
        group_end = index + 1
        while (
            group_end < head_count
            and abs(ranked[group_end - 1].score - ranked[group_end].score)
            <= threshold + threshold_epsilon
        ):
            group_end += 1

        if group_end - index > 1:
            group = ranked[index:group_end]
            original_positions = {row.claim.id: offset for offset, row in enumerate(group)}
            rrf_scores = rrf_fuse(_component_rankings(group), k=k)
            result[index:group_end] = sorted(
                group,
                key=lambda row: (
                    -rrf_scores.get(row.claim.id, 0.0),
                    original_positions[row.claim.id],
                ),
            )

        index = group_end

    return result


def rank_claim_rows(
    query_text: str,
    claims: list[Claim],
    *,
    mode: str = "legacy",
    limit: int = 20,
    vector_hook: VectorSearchHook | None = None,
    semantic_vectors: bool = False,
    query_type: str | None = None,
) -> list[RankedClaim]:
    if mode not in RETRIEVAL_MODES:
        raise ValueError(f"Unknown retrieval mode: {mode}")
    if limit <= 0:
        return []
    if mode == "legacy":
        rows: list[RankedClaim] = []
        for claim in claims:
            lexical = _lexical_score(query_text, claim) if query_text.strip() else 0.0
            confidence = max(0.0, min(1.0, claim.confidence))
            freshness = _freshness_score(claim)
            score = confidence + (get_config().pinned_bonus if claim.pinned else 0.0) + _tier_bonus(claim)
            rows.append(
                RankedClaim(
                    claim=claim,
                    score=score,
                    lexical_score=lexical,
                    freshness_score=freshness,
                    confidence_score=confidence,
                    vector_score=0.0,
                )
            )
        return apply_session_diversity_cap(rows, get_config().session_diversity_cap)[:limit]

    vector_scores: Mapping[int, float] = {}
    if vector_hook is not None:
        vector_scores = vector_hook(query_text, claims) or {}

    vector_enabled = bool(vector_scores)
    cfg = get_config()
    floor_ratio = max(0.0, cfg.boost_floor_ratio)

    # Pass 1: compute each claim's decomposed score (relevance vs. boosts).
    scored: list[tuple[Claim, float, float, float, float, _ScoreParts]] = []
    for claim in claims:
        lexical = _lexical_score(query_text, claim)
        confidence = max(0.0, min(1.0, claim.confidence))
        freshness = _freshness_score(claim)
        vector = max(0.0, min(1.0, float(vector_scores.get(claim.id, 0.0))))
        parts = _compute_score_parts(
            claim, lexical, confidence, freshness, vector, vector_enabled, query_type
        )
        scored.append((claim, lexical, confidence, freshness, vector, parts))

    # Floor-ratio gate: boosts only apply to candidates whose query-relevance
    # is >= floor_ratio * the top relevance. floor_ratio == 0 disables the gate
    # (boosts always apply) — identical to pre-v3.22 behaviour.
    max_relevance = max((p.relevance for *_, p in scored), default=0.0)
    floor = floor_ratio * max_relevance if floor_ratio > 0.0 else 0.0

    ranked: list[RankedClaim] = []
    for claim, lexical, confidence, freshness, vector, parts in scored:
        gated = floor_ratio > 0.0 and max_relevance > 0.0 and parts.relevance < floor
        score = parts.relevance + (0.0 if gated else parts.boosts)
        ranked.append(
            RankedClaim(
                claim=claim,
                score=score,
                lexical_score=lexical,
                freshness_score=freshness,
                confidence_score=confidence,
                vector_score=vector,
                breakdown=_assemble_breakdown(
                    parts=parts,
                    lexical=lexical,
                    confidence=confidence,
                    freshness=freshness,
                    vector=vector,
                    floor=floor,
                    gated=gated,
                    final=score,
                ),
            )
        )

    if query_text.strip():
        max_lexical = max((row.lexical_score for row in ranked), default=0.0)
        if max_lexical > 0.0:
            if semantic_vectors and vector_enabled:
                # With real semantic search, keep results that match on vector OR lexical
                min_vector_threshold = 0.55
                ranked = [
                    row for row in ranked
                    if row.lexical_score > 0.0 or row.vector_score >= min_vector_threshold or row.claim.pinned
                ]
            else:
                ranked = [row for row in ranked if row.lexical_score > 0.0 or row.claim.pinned]

    ranked.sort(
        key=lambda row: (
            row.score,
            row.lexical_score,
            row.confidence_score,
            row.freshness_score,
            row.claim.updated_at,
            row.claim.id,
        ),
        reverse=True,
    )
    cfg = get_config()
    ranked = apply_rrf_tiebreaker(
        ranked,
        threshold=cfg.rrf_tiebreaker_threshold,
        enabled=cfg.rrf_tiebreaker_enabled,
    )
    ranked = apply_session_diversity_cap(ranked, get_config().session_diversity_cap)
    return ranked[:limit]
