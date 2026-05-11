from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.cli import build_parser
from memorymaster.embeddings import EmbeddingProvider
from memorymaster.models import Claim
from memorymaster.service import MemoryService


def _claim(
    claim_id: int,
    text: str,
    *,
    confidence: float = 0.5,
    updated_at: str = "2026-03-01T00:00:00+00:00",
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
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at=updated_at,
        updated_at=updated_at,
        last_validated_at=None,
        archived_at=None,
    )


class _Store:
    db_path = ""

    def __init__(self, claims: list[Claim], vector_scores: dict[int, float] | None = None) -> None:
        self.claims = claims
        self._vector_scores = vector_scores or {}

    def list_claims(self, **kwargs):
        return self.claims[: kwargs.get("limit", len(self.claims))]

    def vector_scores(self, query: str, claims: list[Claim], provider: EmbeddingProvider):
        if not self._vector_scores:
            return {}
        return {claim.id: self._vector_scores.get(claim.id, 0.0) for claim in claims}

    def record_access(self, claim_id: int) -> None:
        return None


def _service(claims: list[Claim], *, vectors: dict[int, float] | None = None, semantic: bool = False) -> MemoryService:
    service = MemoryService.__new__(MemoryService)
    service.store = _Store(claims, vectors)
    service.workspace_root = Path.cwd()
    model = "all-MiniLM-L6-v2" if semantic else "hash-v1"
    service._embedding_provider = EmbeddingProvider(model=model, dims=384)
    service.policy_config = None
    service.tenant_id = None
    service.qdrant = None
    return service


def test_cli_query_and_context_accept_profile() -> None:
    parser = build_parser()

    query_args = parser.parse_args(["query", "alpha", "--profile", "precision"])
    context_args = parser.parse_args(["context", "alpha", "--profile", "fresh"])

    assert query_args.profile == "precision"
    assert context_args.profile == "fresh"


def test_recall_and_precision_profiles_override_weights() -> None:
    lexical = _claim(1, "alpha beta", confidence=0.1)
    confident = _claim(2, "alpha gamma", confidence=0.99)
    service = _service([lexical, confident])

    recall_rows = service.query_rows("alpha beta", retrieval_profile="recall")
    precision_rows = service.query_rows("alpha beta", retrieval_profile="precision")

    assert recall_rows[0]["claim"].id == 1
    assert precision_rows[0]["claim"].id == 2


def test_fresh_profile_prefers_recent_claim() -> None:
    old = _claim(1, "alpha beta", updated_at="2025-01-01T00:00:00+00:00")
    recent = _claim(2, "alpha beta", updated_at="2026-05-11T00:00:00+00:00")
    service = _service([old, recent])

    rows = service.query_rows("alpha beta", retrieval_mode="hybrid", retrieval_profile="fresh")

    assert rows[0]["claim"].id == 2


def test_semantic_profile_is_vector_heavy_when_vectors_are_available() -> None:
    lexical = _claim(1, "alpha beta", confidence=0.5)
    vector_match = _claim(2, "unrelated concept", confidence=0.5)
    service = _service([lexical, vector_match], vectors={1: 0.0, 2: 1.0}, semantic=True)

    rows = service.query_rows("alpha beta", retrieval_mode="hybrid", retrieval_profile="semantic")

    assert rows[0]["claim"].id == 2


def test_unknown_profile_is_rejected() -> None:
    service = _service([_claim(1, "alpha beta")])

    with pytest.raises(ValueError, match="Unknown retrieval profile"):
        service.query_rows("alpha", retrieval_mode="hybrid", retrieval_profile="broad")
