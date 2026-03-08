from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from memorymaster.config import get_config
from memorymaster.models import Claim

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


def rank_claims(
    query_text: str,
    claims: list[Claim],
    *,
    mode: str = "legacy",
    limit: int = 20,
    vector_hook: VectorSearchHook | None = None,
    semantic_vectors: bool = False,
) -> list[Claim]:
    return [row.claim for row in rank_claim_rows(query_text, claims, mode=mode, limit=limit, vector_hook=vector_hook, semantic_vectors=semantic_vectors)]


def rank_claim_rows(
    query_text: str,
    claims: list[Claim],
    *,
    mode: str = "legacy",
    limit: int = 20,
    vector_hook: VectorSearchHook | None = None,
    semantic_vectors: bool = False,
) -> list[RankedClaim]:
    if mode not in RETRIEVAL_MODES:
        raise ValueError(f"Unknown retrieval mode: {mode}")
    if limit <= 0:
        return []
    if mode == "legacy":
        rows: list[RankedClaim] = []
        for claim in claims[:limit]:
            lexical = _lexical_score(query_text, claim) if query_text.strip() else 0.0
            confidence = max(0.0, min(1.0, claim.confidence))
            freshness = _freshness_score(claim)
            score = confidence + (get_config().pinned_bonus if claim.pinned else 0.0)
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
        return rows

    vector_scores: Mapping[int, float] = {}
    if vector_hook is not None:
        vector_scores = vector_hook(query_text, claims) or {}

    vector_enabled = bool(vector_scores)
    ranked: list[RankedClaim] = []

    for claim in claims:
        lexical = _lexical_score(query_text, claim)
        confidence = max(0.0, min(1.0, claim.confidence))
        freshness = _freshness_score(claim)
        vector = max(0.0, min(1.0, float(vector_scores.get(claim.id, 0.0))))

        cfg = get_config()
        if vector_enabled and semantic_vectors:
            # Real semantic embeddings: vector is the primary relevance signal
            score = (0.30 * lexical) + (0.20 * confidence) + (0.10 * freshness) + (0.40 * vector)
        elif vector_enabled:
            # Hash-based vectors: limited semantic value, keep lexical dominant
            w_l, w_c, w_f, w_v = cfg.retrieval_weights
            score = (w_l * lexical) + (w_c * confidence) + (w_f * freshness) + (w_v * vector)
        else:
            w_l, w_c, w_f = cfg.retrieval_weights_no_vector
            score = (w_l * lexical) + (w_c * confidence) + (w_f * freshness)
        if claim.pinned:
            score += cfg.pinned_bonus

        ranked.append(
            RankedClaim(
                claim=claim,
                score=score,
                lexical_score=lexical,
                freshness_score=freshness,
                confidence_score=confidence,
                vector_score=vector,
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
    return ranked[:limit]
