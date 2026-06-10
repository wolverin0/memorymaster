from __future__ import annotations

from memorymaster.config import get_config, reset_config
from memorymaster.models import Claim
from memorymaster.recall.retrieval import RankedClaim, apply_rrf_tiebreaker


def _claim(claim_id: int) -> Claim:
    return Claim(
        id=claim_id,
        text=f"claim {claim_id}",
        idempotency_key=None,
        normalized_text=None,
        claim_type=None,
        subject=None,
        predicate=None,
        object_value=None,
        scope="project",
        volatility="medium",
        status="confirmed",
        confidence=0.8,
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-03-01T00:00:00+00:00",
        updated_at="2026-03-08T00:00:00+00:00",
        last_validated_at=None,
        archived_at=None,
    )


def _row(
    claim_id: int,
    *,
    score: float,
    lexical: float,
    vector: float,
    confidence: float,
    freshness: float,
) -> RankedClaim:
    return RankedClaim(
        claim=_claim(claim_id),
        score=score,
        lexical_score=lexical,
        vector_score=vector,
        confidence_score=confidence,
        freshness_score=freshness,
    )


def test_clear_winner_pair_unchanged() -> None:
    rows = [
        _row(1, score=0.8, lexical=0.1, vector=0.1, confidence=0.1, freshness=0.1),
        _row(2, score=0.5, lexical=1.0, vector=1.0, confidence=1.0, freshness=1.0),
    ]

    result = apply_rrf_tiebreaker(rows, threshold=0.01, enabled=True)

    assert [row.claim.id for row in result] == [1, 2]


def test_near_tie_reordered_by_rrf() -> None:
    rows = [
        _row(1, score=0.81, lexical=1.0, vector=0.0, confidence=0.0, freshness=0.0),
        _row(2, score=0.80, lexical=0.9, vector=1.0, confidence=1.0, freshness=1.0),
    ]

    result = apply_rrf_tiebreaker(rows, threshold=0.01, enabled=True)

    assert [row.claim.id for row in result] == [2, 1]


def test_disabled_flag_no_op(monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_RRF_TIEBREAKER", "0")
    reset_config()
    rows = [
        _row(1, score=0.81, lexical=1.0, vector=0.0, confidence=0.0, freshness=0.0),
        _row(2, score=0.80, lexical=0.9, vector=1.0, confidence=1.0, freshness=1.0),
    ]

    result = apply_rrf_tiebreaker(rows, threshold=0.01, enabled=get_config().rrf_tiebreaker_enabled)

    assert [row.claim.id for row in result] == [1, 2]
    reset_config()
