"""Unit tests for ``memorymaster.recall_fusion.rrf_fuse``.

Smoke tests only — wiring into ``context_hook.recall`` is covered by the
end-to-end eval in ``scripts/eval_recall_precision_at_5.py``.
"""

from __future__ import annotations

import pytest

from memorymaster.recall_fusion import RRF_K_DEFAULT, rrf_fuse


def test_smoke_two_streams_three_items() -> None:
    """Two streams, three items — claim present in both streams wins."""
    rankings = {
        "bm25": [1, 2, 3],
        "entity": [2, 1],
    }
    scores = rrf_fuse(rankings, k=60)

    # Claim 2 is at rank 1 in entity and rank 2 in bm25.
    # Claim 1 is at rank 1 in bm25 and rank 2 in entity.
    # Both appear in both streams, so both score higher than claim 3.
    assert scores[2] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[1] == pytest.approx(1 / 61 + 1 / 62)
    # Claim 3 only in bm25 at rank 3.
    assert scores[3] == pytest.approx(1 / 63)
    assert scores[2] > scores[3]
    assert scores[1] > scores[3]


def test_identity_single_stream_preserves_order() -> None:
    """With only one stream, the resulting ordering must equal the input."""
    rankings = {"bm25": [10, 20, 30, 40, 50]}
    scores = rrf_fuse(rankings, k=60)
    ranked = sorted(scores, key=scores.get, reverse=True)
    assert ranked == [10, 20, 30, 40, 50]


def test_monotonic_decrease_within_stream() -> None:
    """Within a single stream, score must strictly decrease as rank grows."""
    rankings = {"bm25": list(range(100, 110))}
    scores = rrf_fuse(rankings, k=60)
    claim_ids_in_rank_order = list(range(100, 110))
    for i in range(len(claim_ids_in_rank_order) - 1):
        higher = scores[claim_ids_in_rank_order[i]]
        lower = scores[claim_ids_in_rank_order[i + 1]]
        assert higher > lower, f"expected rank {i+1} > rank {i+2}"


def test_empty_stream_contributes_nothing() -> None:
    """An empty ranking list is a no-op (skipped, not an error)."""
    rankings = {"bm25": [1, 2], "vector": []}
    scores = rrf_fuse(rankings, k=60)
    only_bm25 = rrf_fuse({"bm25": [1, 2]}, k=60)
    assert scores == only_bm25


def test_empty_input_returns_empty() -> None:
    """No streams at all → empty score map."""
    assert rrf_fuse({}, k=60) == {}


def test_duplicate_claim_across_streams_sums() -> None:
    """A claim listed in N streams gets the sum of N contributions."""
    rankings = {"a": [1], "b": [1], "c": [1]}
    scores = rrf_fuse(rankings, k=60)
    assert scores[1] == pytest.approx(3 * (1 / 61))


def test_k_parameter_affects_scale() -> None:
    """Smaller k → sharper head; contributions drop faster."""
    small_k = rrf_fuse({"s": [1, 2]}, k=1)
    big_k = rrf_fuse({"s": [1, 2]}, k=1000)
    # Head-to-tail ratio is sharper for small k.
    small_ratio = small_k[1] / small_k[2]
    big_ratio = big_k[1] / big_k[2]
    assert small_ratio > big_ratio


def test_invalid_k_raises() -> None:
    with pytest.raises(ValueError):
        rrf_fuse({"s": [1]}, k=0)
    with pytest.raises(ValueError):
        rrf_fuse({"s": [1]}, k=-5)


def test_default_k_is_classical_60() -> None:
    """Shipped default matches Cormack et al. (2009)."""
    assert RRF_K_DEFAULT == 60
    explicit = rrf_fuse({"s": [1]}, k=60)
    default = rrf_fuse({"s": [1]})
    assert explicit == default
