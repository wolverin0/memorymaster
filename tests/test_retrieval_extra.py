"""Extra retrieval coverage tests (track T21).

These tests target the under-exercised branches of the hybrid retrieval
ranking layer in ``memorymaster/retrieval.py``. Each test anchors on the
recall-quality REASON the branch exists, not merely on line execution:

  - the weighted blend must respect lexical/vector weighting,
  - the floor-ratio gate must suppress metadata boosts on weak matches
    (and stay disabled — original behaviour — when the ratio is 0),
  - the RRF tiebreaker must reorder a near-tie group by cross-signal rank
    agreement while leaving clear winners and len<2 lists untouched,
  - the session-diversity cap must stop one chatty session from
    monopolising results without dropping unique-session items.
"""

from __future__ import annotations

import pytest

from memorymaster.core.config import get_config, reset_config
from memorymaster.core.models import Claim
from memorymaster.recall.retrieval import (
    RankedClaim,
    apply_rrf_tiebreaker,
    apply_session_diversity_cap,
    rank_claim_rows,
)


@pytest.fixture(autouse=True)
def _reset_config():
    reset_config()
    yield
    reset_config()


def _claim(
    claim_id: int,
    *,
    text: str = "alpha beta retrieval target",
    source_agent: str | None = None,
    pinned: bool = False,
    confidence: float = 0.8,
) -> Claim:
    return Claim(
        id=claim_id,
        text=text,
        idempotency_key=None,
        normalized_text=None,
        claim_type=None,
        subject=None,
        predicate=None,
        object_value=None,
        scope="project",
        volatility="medium",
        status="confirmed",
        confidence=confidence,
        pinned=pinned,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2999-01-01T00:00:00+00:00",
        updated_at="2999-01-01T00:00:00+00:00",
        last_validated_at=None,
        archived_at=None,
        source_agent=source_agent,
    )


def _row(claim_id: int, *, score: float, source_agent: str | None = None) -> RankedClaim:
    return RankedClaim(
        claim=_claim(claim_id, source_agent=source_agent),
        score=score,
        lexical_score=0.0,
        vector_score=0.0,
        confidence_score=0.0,
        freshness_score=0.0,
    )


def _vector_hook(scores: dict[int, float]):
    def hook(query, claims):
        return {c.id: scores.get(c.id, 0.0) for c in claims}

    return hook


# --------------------------------------------------------------------------
# Ranking blend (weighted sum of lexical + vector relevance)
# --------------------------------------------------------------------------


def test_blend_higher_vector_weight_raises_score():
    """The vector signal must actually move the final score through its
    configured weight. With the default profile (w_vec=0.40) a vector-strong
    claim outscores its no-vector baseline by exactly w_vec * vector — proving
    the blend is a weighted sum, not a pass-through of lexical only."""
    claim = _claim(1)
    with_vector = rank_claim_rows(
        "alpha beta",
        [claim],
        mode="hybrid",
        limit=1,
        vector_hook=_vector_hook({1: 0.5}),
    )[0]
    without_vector = rank_claim_rows(
        "alpha beta",
        [claim],
        mode="hybrid",
        limit=1,
        vector_hook=_vector_hook({1: 0.0}),
    )[0]
    # vector_enabled is True in both calls (hook returns a dict); only the
    # magnitude differs, so the delta is purely w_vec * 0.5.
    assert with_vector.score - without_vector.score == pytest.approx(0.40 * 0.5)


def test_blend_no_vector_path_ignores_vector_weight():
    """When the vector hook yields an empty mapping, vector_enabled is False
    and the no-vector weight tuple is used — the vector score must contribute
    nothing even if a stray value were present. This guards the branch that
    keeps lexical-only deployments correct."""
    row = rank_claim_rows(
        "alpha beta",
        [_claim(1)],
        mode="hybrid",
        limit=1,
        vector_hook=lambda q, c: {},
    )[0]
    assert row.vector_score == 0.0
    assert row.breakdown["weights"][3] == 0.0  # w_v forced to 0


# --------------------------------------------------------------------------
# Floor-ratio gate edges
# --------------------------------------------------------------------------


def test_floor_ratio_zero_keeps_boosts(monkeypatch):
    """floor_ratio == 0 disables the gate: every candidate keeps its metadata
    boosts, identical to pre-v3.22 behaviour. This is the default and must not
    regress."""
    reset_config()  # ensure default 0.0
    rows = rank_claim_rows(
        "alpha beta",
        [_claim(1, text="alpha beta match"), _claim(2, text="zzz unrelated")],
        mode="hybrid",
        limit=5,
        vector_hook=_vector_hook({1: 0.2, 2: 0.2}),
    )
    by_id = {r.claim.id: r for r in rows}
    # The weak (id=2) claim is filtered out by the lexical post-filter (no
    # lexical overlap, vector below 0.55), so only id=1 survives; its boosts
    # were applied because the gate is disabled.
    assert 1 in by_id
    assert by_id[1].breakdown["boosts_applied"] is True


def test_floor_ratio_gates_boosts_on_weak_relevance(monkeypatch):
    """With a positive floor_ratio, a claim whose relevance is far below the
    top match must have its metadata boosts (confidence/freshness/tier)
    suppressed — that is the entire point of the gate: don't let a stale
    high-confidence claim ride into the results on boosts alone."""
    monkeypatch.setenv("MEMORYMASTER_BOOST_FLOOR_RATIO", "0.5")
    reset_config()
    assert get_config().boost_floor_ratio == 0.5

    strong = _claim(1, text="alpha beta gamma delta", confidence=0.1)
    weak = _claim(2, text="alpha", confidence=1.0)  # low overlap, high conf
    rows = rank_claim_rows(
        "alpha beta gamma delta",
        [strong, weak],
        mode="hybrid",
        limit=5,
        vector_hook=_vector_hook({1: 0.9, 2: 0.0}),
    )
    by_id = {r.claim.id: r for r in rows}
    assert by_id[2].breakdown["boosts_applied"] is False
    # The gated claim's final score equals its relevance alone (no boosts).
    assert by_id[2].breakdown["final"] == pytest.approx(by_id[2].breakdown["relevance"])


# --------------------------------------------------------------------------
# RRF tiebreaker edges
# --------------------------------------------------------------------------


def test_rrf_short_circuits_for_single_item():
    """A list shorter than 2 has no ties to break — the function must return
    it untouched (the len<2 guard), never crashing on the empty/singleton
    edge."""
    one = [_row(1, score=0.9)]
    assert apply_rrf_tiebreaker(one, enabled=True) is one
    assert apply_rrf_tiebreaker([], enabled=True) == []


def test_rrf_disabled_returns_input_identity():
    """When disabled, the tiebreaker is a strict no-op (returns the same
    object), so callers can wire it in unconditionally with zero cost when the
    flag is off."""
    rows = [_row(1, score=0.80), _row(2, score=0.80)]
    assert apply_rrf_tiebreaker(rows, enabled=False) is rows


def test_rrf_reorders_only_within_tie_group_below_threshold():
    """Scores within ``threshold`` form a tie group that RRF reorders by
    cross-signal rank; scores outside the threshold keep their original order.
    Here ids 1 and 2 tie (|0.80-0.799| < 0.01) and get fused, while id 3
    (0.50) is a clear loser and stays last."""
    group = [
        RankedClaim(
            claim=_claim(1),
            score=0.800,
            lexical_score=0.2,
            vector_score=0.0,
            confidence_score=0.0,
            freshness_score=0.0,
        ),
        RankedClaim(
            claim=_claim(2),
            score=0.799,
            lexical_score=0.9,
            vector_score=1.0,
            confidence_score=1.0,
            freshness_score=1.0,
        ),
        RankedClaim(
            claim=_claim(3),
            score=0.500,
            lexical_score=1.0,
            vector_score=1.0,
            confidence_score=1.0,
            freshness_score=1.0,
        ),
    ]
    result = apply_rrf_tiebreaker(group, threshold=0.01, enabled=True)
    ids = [r.claim.id for r in result]
    assert ids[0] == 2, "stronger cross-signal claim promoted within tie group"
    assert ids[-1] == 3, "clear loser outside the tie group keeps last place"


# --------------------------------------------------------------------------
# Session-diversity cap edges
# --------------------------------------------------------------------------


def test_session_cap_disabled_when_non_positive():
    """cap <= 0 disables capping entirely (the default), so nothing is dropped
    — used when the caller wants the raw ranking."""
    rows = [
        _row(1, score=0.9, source_agent="a"),
        _row(2, score=0.8, source_agent="a"),
        _row(3, score=0.7, source_agent="a"),
    ]
    assert apply_session_diversity_cap(rows, 0) is rows
    assert apply_session_diversity_cap(rows, -1) is rows


def test_session_cap_trims_each_session_independently():
    """The cap is per source_agent session, so two busy sessions each keep up
    to ``cap`` items — the limit is not global. This is what prevents one
    chatty agent from crowding out another agent's best claims."""
    rows = [
        _row(1, score=0.90, source_agent="a"),
        _row(2, score=0.85, source_agent="a"),
        _row(3, score=0.80, source_agent="a"),  # 3rd from 'a' -> dropped
        _row(4, score=0.70, source_agent="b"),
        _row(5, score=0.60, source_agent="b"),
        _row(6, score=0.50, source_agent="b"),  # 3rd from 'b' -> dropped
    ]
    capped = apply_session_diversity_cap(rows, 2)
    assert [r.claim.id for r in capped] == [1, 2, 4, 5]


def test_session_cap_distinct_sessions_all_survive():
    """When every item is from a different session, none exceed the cap and
    all survive — the cap must never penalise diversity it is meant to
    protect."""
    rows = [
        _row(1, score=0.9, source_agent="a"),
        _row(2, score=0.8, source_agent="b"),
        _row(3, score=0.7, source_agent="c"),
    ]
    capped = apply_session_diversity_cap(rows, 1)
    assert [r.claim.id for r in capped] == [1, 2, 3]


def test_session_cap_falls_back_to_claim_id_key_when_no_source():
    """With no source_agent / citations / subject, each claim is keyed by its
    own id (``claim:<id>``), so distinct claims are treated as distinct
    sessions and none are capped away — avoids collapsing unrelated claims
    into one bucket."""
    rows = [_row(1, score=0.9), _row(2, score=0.8)]  # source_agent=None
    capped = apply_session_diversity_cap(rows, 1)
    assert [r.claim.id for r in capped] == [1, 2]


# --------------------------------------------------------------------------
# Full pipeline interaction through rank_claim_rows
# --------------------------------------------------------------------------


def test_rank_claim_rows_applies_session_cap_end_to_end(monkeypatch):
    """End-to-end: with a session cap of 1, two equally-relevant claims from
    the SAME source_agent collapse to one in the final ranking — proving the
    cap stage runs inside rank_claim_rows, not just as a standalone helper."""
    monkeypatch.setenv("MEMORYMASTER_SESSION_DIVERSITY_CAP", "1")
    reset_config()
    assert get_config().session_diversity_cap == 1

    rows = rank_claim_rows(
        "alpha beta",
        [
            _claim(1, source_agent="same"),
            _claim(2, source_agent="same"),
        ],
        mode="hybrid",
        limit=5,
        vector_hook=_vector_hook({1: 0.6, 2: 0.6}),
    )
    assert len(rows) == 1, "second same-session claim capped out"
