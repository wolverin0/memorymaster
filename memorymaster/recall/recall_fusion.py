"""Reciprocal Rank Fusion (RRF) for multi-stream retrieval.

Combines several ranked streams (BM25, entity-fanout, vector, verbatim,
freshness, etc.) into a single consensus ranking without needing to
normalise or weight their raw scores.

RRF is a classic, robust fusion technique: each stream contributes
``1 / (k + rank)`` for every document it ranks, summed across streams.
Reference: Cormack, Clarke, Buttcher (2009), "Reciprocal rank fusion
outperforms Condorcet and individual rank learning methods."

This module is intentionally tiny, dependency-free, and has a strict
read-only contract with the caller — the caller owns all stream
materialisation and chooses whether to actually apply the RRF result.

Usage:
    from memorymaster.recall.recall_fusion import rrf_fuse

    rankings = {
        "bm25":    [claim_id_42, claim_id_7, claim_id_99],
        "entity":  [claim_id_7, claim_id_12],
        "vector":  [claim_id_99, claim_id_42, claim_id_7],
    }
    scores = rrf_fuse(rankings, k=60)
    ranked_ids = sorted(scores, key=scores.get, reverse=True)
"""

from __future__ import annotations

from dataclasses import dataclass


# Classic RRF constant from Cormack et al. (2009). Dampens the head of
# each ranking so a single strong stream can't dominate. 60 is the
# empirically chosen default that works across IR benchmarks.
RRF_K_DEFAULT: int = 60


@dataclass(frozen=True)
class StreamContribution:
    """Per-stream contribution to a document's RRF score (diagnostics)."""

    stream: str
    rank: int  # 1-based rank within the stream
    contribution: float  # 1 / (k + rank)


def rrf_fuse(
    rankings: dict[str, list[int]],
    k: int = RRF_K_DEFAULT,
) -> dict[int, float]:
    """Reciprocal Rank Fusion across named streams.

    Args:
        rankings: map of ``stream_name -> ordered list of claim_ids``
            where position 0 = rank 1 (best). Empty lists are ignored.
        k: RRF dampening constant. Must be >= 1. 60 is the classical
            default and the shipped value in this project.

    Returns:
        ``{claim_id: rrf_score}`` where
        ``score = sum over streams of 1 / (k + rank_in_stream)``.
        Claims that appear in zero streams are not in the result.

    Raises:
        ValueError: if ``k < 1``.
    """
    if k < 1:
        raise ValueError(f"RRF k must be >= 1, got {k}")

    scores: dict[int, float] = {}
    for stream_name, ranked_ids in rankings.items():
        if not ranked_ids:
            continue
        for position, claim_id in enumerate(ranked_ids):
            rank = position + 1  # RRF uses 1-based ranks
            contribution = 1.0 / (k + rank)
            scores[claim_id] = scores.get(claim_id, 0.0) + contribution
    return scores
