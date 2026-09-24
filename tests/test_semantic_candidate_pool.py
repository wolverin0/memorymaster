"""Regression coverage for bounded hybrid candidate admission.

Hybrid ranking can only promote claims it receives.  The candidate set therefore
has to be broad enough for semantic matches and also admit lexical matches that
fall beyond its recency/confidence window, while keeping the authorization
filters in front of embedding work.
"""
from __future__ import annotations

from memorymaster.core.models import CitationInput
from memorymaster.recall import candidate_pool as candidate_module
from memorymaster.core.service import MemoryService
from memorymaster.core.security import is_sensitive_claim


QUERY = "how does the satellite relay authenticate"


class _DeterministicSemanticProvider:
    """Local provider that records every text sent for embedding."""

    model = "test-semantic-candidate-pool-v1"
    is_semantic = True

    def __init__(self) -> None:
        self.query_inputs: list[str] = []
        self.claim_inputs: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.query_inputs.append(text)
        assert text == QUERY
        return [1.0, 0.0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.claim_inputs.extend(texts)
        return [
            [1.0, 0.0] if "SEMANTIC-TARGET" in text else [0.0, 1.0]
            for text in texts
        ]


def _claim(service: MemoryService, text: str, *, confidence: float, **kwargs):
    params = {
        "citations": [CitationInput(source="synthetic-test")],
        "confidence": confidence,
        "tenant_id": "tenant-a",
        "scope": "project:allowed",
    }
    params.update(kwargs)
    return service.store.create_claim(
        text=text,
        **params,
    )


def test_hybrid_candidate_pool_admits_semantic_and_lexical_hits_before_embedding(
    tmp_path,
):
    """High-confidence noise must not hide either class of useful candidate.

    The semantic target is outside the historical 60-row window.  The lexical
    target is outside the new broad pool too, so it proves that the query-driven
    lexical union is required.  Rows outside tenant/scope/visibility/sensitivity
    and temporal authority must never be embedded.
    """
    service = MemoryService(
        tmp_path / "candidate-pool.db",
        workspace_root=tmp_path,
        tenant_id="tenant-a",
    )
    service.init_db()

    # These fill the old 60-row candidate window but have neither lexical nor
    # semantic relevance to QUERY.
    for index in range(61):
        _claim(service, f"NOISE-HIGH-{index}", confidence=0.99)

    semantic = _claim(
        service,
        "SEMANTIC-TARGET lunar certificate exchange",
        confidence=0.80,
    )
    # Push the lexical result beyond the bounded broad pool while leaving the
    # semantic result within it.  The query-specific FTS union must recover it.
    for index in range(2_010):
        _claim(service, f"NOISE-LOW-{index}", confidence=0.70)
    lexical = _claim(
        service,
        f"LEXICAL-TARGET {QUERY}",
        confidence=0.10,
    )

    wrong_tenant = service.store.create_claim(
        text="BLOCK-TENANT",
        citations=[CitationInput(source="synthetic-test")],
        confidence=1.0,
        tenant_id="tenant-b",
        scope="project:allowed",
    )
    wrong_scope = _claim(service, "BLOCK-SCOPE", confidence=1.0, scope="project:other")
    private = _claim(
        service,
        "BLOCK-PRIVATE",
        confidence=1.0,
        source_agent="other-agent",
        visibility="private",
    )
    sensitive = _claim(
        service,
        "BLOCK-SENSITIVE AKIAIOSFODNN7EXAMPLE",
        confidence=1.0,
    )
    assert is_sensitive_claim(sensitive), "test seed must exercise sensitive filtering"
    expired = _claim(
        service,
        "BLOCK-EXPIRED",
        confidence=1.0,
        valid_until="2000-01-01T00:00:00Z",
    )

    provider = _DeterministicSemanticProvider()
    service.embedding_provider = provider
    rows = service.query_rows(
        QUERY,
        retrieval_mode="hybrid",
        include_candidates=True,
        # The lexical reservation must survive a large displayed limit too;
        # otherwise the broad stream would consume the total cap first.
        limit=100,
        scope_allowlist=["project:allowed"],
        requesting_agent="reader-agent",
    )

    returned_ids = {row["claim"].id for row in rows}
    assert {semantic.id, lexical.id} <= returned_ids
    assert provider.query_inputs == [QUERY], "hybrid retrieval embeds the query once"
    assert len(provider.claim_inputs) <= 2_000, "embedding work stays bounded"
    embedded = "\n".join(provider.claim_inputs)
    assert "SEMANTIC-TARGET" in embedded
    assert "LEXICAL-TARGET" in embedded
    for blocked in (wrong_tenant, wrong_scope, private, sensitive, expired):
        assert blocked.text not in embedded


def test_hybrid_candidate_pool_replenishes_after_authorization_filters(
    tmp_path, monkeypatch,
):
    """Foreign-private rows cannot starve the bounded semantic admission pool."""
    monkeypatch.setattr(candidate_module, "_HYBRID_CANDIDATE_POOL_MIN", 2)
    monkeypatch.setattr(candidate_module, "_HYBRID_CANDIDATE_POOL_CAP", 4)
    monkeypatch.setattr(candidate_module, "_HYBRID_CANDIDATE_PAGE_SIZE", 3)
    service = MemoryService(
        tmp_path / "candidate-replenishment.db",
        workspace_root=tmp_path,
        tenant_id="tenant-a",
    )
    service.init_db()
    for index in range(15):
        _claim(
            service,
            f"PRIVATE-STARVATION-{index}",
            confidence=0.99,
            source_agent="other-agent",
            visibility="private",
        )
    semantic = _claim(
        service,
        "SEMANTIC-TARGET replenished after authorization filtering",
        confidence=0.20,
    )

    provider = _DeterministicSemanticProvider()
    service.embedding_provider = provider
    rows = service.query_rows(
        QUERY,
        retrieval_mode="hybrid",
        include_candidates=True,
        limit=1,
        scope_allowlist=["project:allowed"],
        requesting_agent="reader-agent",
    )

    assert [row["claim"].id for row in rows] == [semantic.id]
    embedded = "\n".join(provider.claim_inputs)
    assert "SEMANTIC-TARGET" in embedded
    assert "PRIVATE-STARVATION" not in embedded


def test_planner_or_expansion_has_a_bounded_deduplicated_query_count(tmp_path, monkeypatch):
    service = MemoryService(tmp_path / "or-fanout.db", workspace_root=tmp_path)
    service.init_db()
    queries = []

    def list_claims(**kwargs):
        queries.append(kwargs["text_query"])
        return []

    monkeypatch.setattr(service.store, "list_claims", list_claims)
    query = " OR ".join(["repeat"] * 50 + [f"term-{index}" for index in range(100)])
    assert service._legacy_candidates(query, 60, ["confirmed"], None) == []
    assert len(queries) == 32
    assert queries[0] == "repeat"
    assert len(set(queries)) == 32
