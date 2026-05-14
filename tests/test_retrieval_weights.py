from __future__ import annotations

import pytest

from memorymaster.config import reset_config
from memorymaster.models import Claim
from memorymaster.retrieval import rank_claim_rows


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


def _row(*, semantic_vectors: bool):
    def vector_hook(query, claims):
        return {claim.id: 0.6 for claim in claims}

    return rank_claim_rows(
        "alpha beta",
        [_claim()],
        mode="hybrid",
        limit=1,
        vector_hook=vector_hook,
        semantic_vectors=semantic_vectors,
    )[0]


def test_memorymaster_w_lex_affects_vector_enabled_ranking_paths(monkeypatch):
    default_lexical = _row(semantic_vectors=False)
    default_semantic = _row(semantic_vectors=True)

    monkeypatch.setenv("MEMORYMASTER_W_LEX", "0.55")
    reset_config()

    override_lexical = _row(semantic_vectors=False)
    override_semantic = _row(semantic_vectors=True)
    expected_delta = (0.55 - 0.30) * default_lexical.lexical_score

    assert override_lexical.score - default_lexical.score == pytest.approx(expected_delta)
    assert override_semantic.score - default_semantic.score == pytest.approx(expected_delta)
    assert override_lexical.score != default_lexical.score
    assert override_semantic.score != default_semantic.score
