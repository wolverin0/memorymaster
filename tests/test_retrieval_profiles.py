"""S3 regression tests — per-question-type retrieval weight profiles.

Verifies:
1. With NO profile env var set, query_type is a no-op (back-compat).
2. With a per-type profile env var set, _compute_claim_score swaps the
   weight tuple for queries of that type.
3. query_type that does NOT match any configured profile falls back to
   the global retrieval_weights (default behaviour preserved).
4. The env-var slug mapping (UPPER_SNAKE -> lower-hyphen) round-trips
   correctly so LongMemEval-S labels like 'single-session-preference'
   are reachable via MEMORYMASTER_RETRIEVAL_PROFILE_SINGLE_SESSION_PREFERENCE.
"""
from __future__ import annotations

import pytest

from memorymaster.config import reset_config, get_config
from memorymaster.models import Claim
from memorymaster.recall.retrieval import rank_claim_rows


@pytest.fixture(autouse=True)
def _reset_config():
    reset_config()
    yield
    reset_config()


def _claim() -> Claim:
    return Claim(
        id=1,
        text="alpha beta retrieval target",
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
        created_at="2999-01-01T00:00:00+00:00",
        updated_at="2999-01-01T00:00:00+00:00",
        last_validated_at=None,
        archived_at=None,
    )


def _rank(*, semantic_vectors: bool, query_type: str | None = None):
    def vector_hook(query, claims):
        return {c.id: 0.6 for c in claims}

    return rank_claim_rows(
        "alpha beta",
        [_claim()],
        mode="hybrid",
        limit=1,
        vector_hook=vector_hook,
        semantic_vectors=semantic_vectors,
        query_type=query_type,
    )[0]


def test_query_type_without_profile_is_noop():
    """Passing query_type when no profile env var is set must not change
    scores — back-compat with all existing callers."""
    default = _rank(semantic_vectors=False)
    typed = _rank(semantic_vectors=False, query_type="single-session-preference")
    assert typed.score == pytest.approx(default.score)


def test_profile_env_var_overrides_default_weights(monkeypatch):
    """When a profile env var matches the query_type, weights swap.

    Default vec-enabled weights: (lex=0.30, conf=0.20, fresh=0.10, vec=0.40).
    Profile under test: (lex=0.10, conf=0.10, fresh=0.10, vec=0.70) — heavy
    on vector, which is the predicted direction for ``single-session-preference``
    where lexical matching is structurally weak (preferences are paraphrased).
    """
    monkeypatch.setenv(
        "MEMORYMASTER_RETRIEVAL_PROFILE_SINGLE_SESSION_PREFERENCE",
        "0.10,0.10,0.10,0.70",
    )
    reset_config()

    cfg = get_config()
    assert cfg.retrieval_profile("single-session-preference") == (0.10, 0.10, 0.10, 0.70)

    default = _rank(semantic_vectors=False)
    typed = _rank(semantic_vectors=False, query_type="single-session-preference")
    assert typed.score != default.score


def test_profile_does_not_leak_to_other_query_types(monkeypatch):
    """Profile under one type must not affect queries with a different type
    (or with no type at all)."""
    monkeypatch.setenv(
        "MEMORYMASTER_RETRIEVAL_PROFILE_SINGLE_SESSION_PREFERENCE",
        "0.10,0.10,0.10,0.70",
    )
    reset_config()

    default = _rank(semantic_vectors=False)
    other_type = _rank(semantic_vectors=False, query_type="multi-session")
    none_type = _rank(semantic_vectors=False, query_type=None)

    assert other_type.score == pytest.approx(default.score)
    assert none_type.score == pytest.approx(default.score)


def test_profile_slug_round_trips_underscores_to_hyphens(monkeypatch):
    """Env var SINGLE_SESSION_PREFERENCE must be reachable as the canonical
    bench label 'single-session-preference'."""
    monkeypatch.setenv(
        "MEMORYMASTER_RETRIEVAL_PROFILE_TEMPORAL_REASONING",
        "0.20,0.10,0.40,0.30",
    )
    reset_config()

    cfg = get_config()
    assert cfg.retrieval_profile("temporal-reasoning") == (0.20, 0.10, 0.40, 0.30)
    # And the env-form lookup (uppercased, underscored) finds nothing — only
    # the canonical lower-hyphen form is the key.
    assert cfg.retrieval_profile("TEMPORAL_REASONING") is None


def test_profile_works_in_semantic_path(monkeypatch):
    """The semantic_vectors=True branch must also honour the profile —
    same blend formula, just different signal source."""
    monkeypatch.setenv(
        "MEMORYMASTER_RETRIEVAL_PROFILE_SINGLE_SESSION_PREFERENCE",
        "0.10,0.10,0.10,0.70",
    )
    reset_config()

    default = _rank(semantic_vectors=True)
    typed = _rank(semantic_vectors=True, query_type="single-session-preference")
    assert typed.score != default.score
