from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from memorymaster.models import Claim

RETRIEVAL_MODES = ("legacy", "hybrid")

VectorSearchHook = Callable[[str, list[Claim]], Mapping[int, float]]

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_FRESHNESS_HALF_LIFE_HOURS = {
    "low": 168.0,
    "medium": 72.0,
    "high": 24.0,
}
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

    score = (0.55 * token_recall) + (0.15 * token_precision) + (0.25 * contains_phrase) + (0.05 * prefix_bonus)
    return max(0.0, min(1.0, score))


def _freshness_score(claim: Claim) -> float:
    anchor = _parse_iso(claim.last_validated_at) or _parse_iso(claim.updated_at) or _parse_iso(claim.created_at)
    if anchor is None:
        return 0.5
    now = datetime.now(timezone.utc)
    age_hours = max(0.0, (now - anchor).total_seconds() / 3600.0)
    half_life = _FRESHNESS_HALF_LIFE_HOURS.get(claim.volatility, _FRESHNESS_HALF_LIFE_HOURS["medium"])
    return max(0.0, min(1.0, math.exp(-age_hours / max(1.0, half_life))))


def rank_claims(
    query_text: str,
    claims: list[Claim],
    *,
    mode: str = "legacy",
    limit: int = 20,
    vector_hook: VectorSearchHook | None = None,
) -> list[Claim]:
    return [row.claim for row in rank_claim_rows(query_text, claims, mode=mode, limit=limit, vector_hook=vector_hook)]


def rank_claim_rows(
    query_text: str,
    claims: list[Claim],
    *,
    mode: str = "legacy",
    limit: int = 20,
    vector_hook: VectorSearchHook | None = None,
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
            score = confidence + (0.03 if claim.pinned else 0.0)
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

        if vector_enabled:
            score = (0.45 * lexical) + (0.30 * confidence) + (0.15 * freshness) + (0.10 * vector)
        else:
            score = (0.55 * lexical) + (0.30 * confidence) + (0.15 * freshness)
        if claim.pinned:
            score += 0.03

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
