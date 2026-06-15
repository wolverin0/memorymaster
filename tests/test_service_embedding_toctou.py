"""Regression test for the embeddings semantic-filter TOCTOU (audit, batch 2 follow-up).

WHY this matters: retrieval applies a LENIENT vector-only filter (keep any claim
with vector_score >= 0.55, even with zero lexical overlap) only when
``semantic_vectors=True``. The Gemini embedding backend downgrades to hash
embeddings LAZILY — the fallback fires inside the vector hook on the first embed,
and ``is_semantic`` only reflects it afterwards. service.query_rows used to read
``is_semantic`` BEFORE any embed ran, so a provider that degraded mid-flight was
still reported as semantic and the lenient filter was applied to non-semantic
hash vectors, surfacing irrelevant claims.

The fix probes one embed (the hook embeds the query anyway) to resolve the lazy
downgrade before choosing the filter. These tests anchor the requirement
(degraded provider => non-semantic filter), not the implementation.
"""
from __future__ import annotations

from pathlib import Path


from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService

TOKEN = "toctoutoken"


def _service(tmp_path: Path, monkeypatch) -> MemoryService:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    svc = MemoryService(str(tmp_path / "memory.db"), workspace_root=tmp_path)
    svc.init_db()
    svc.ingest(
        text=f"{TOKEN} a claim worth ranking here",
        citations=[CitationInput(source="t", locator="t", excerpt="x")],
        scope="project:foo",
        claim_type="fact",
        source_agent="agentA",
    )
    return svc


class _DegradingProvider:
    """Claims to be semantic, then degrades to hash on the first embed (mimics a
    runtime Gemini failure that falls back to hash mid-flight)."""

    def __init__(self) -> None:
        self._degraded = False
        self.embed_calls = 0

    @property
    def is_semantic(self) -> bool:
        return not self._degraded

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        self._degraded = True
        return [0.0] * 8


class _StableSemanticProvider:
    def __init__(self) -> None:
        self.embed_calls = 0

    @property
    def is_semantic(self) -> bool:
        return True

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        return [0.1] * 8


def _capture_semantic(svc: MemoryService, monkeypatch) -> dict:
    captured: dict = {}

    def fake_rank(*args, **kwargs):
        captured["semantic_vectors"] = kwargs.get("semantic_vectors")
        return []

    monkeypatch.setattr("memorymaster.core.service.rank_claim_rows", fake_rank)
    # Force the hybrid vector path: vector_hook is None + store exposes vector_scores.
    svc.store.vector_scores = lambda text, claims, provider: {}
    return captured


def test_degraded_provider_disables_semantic_filter(tmp_path, monkeypatch):
    """A provider that degrades to hash during ranking must NOT be treated as
    semantic — otherwise the lenient vector-only filter runs against hash vectors."""
    svc = _service(tmp_path, monkeypatch)
    provider = _DegradingProvider()
    svc.embedding_provider = provider
    captured = _capture_semantic(svc, monkeypatch)

    svc.query_rows(query_text=TOKEN, retrieval_mode="hybrid", include_candidates=True, limit=10)

    assert provider.embed_calls >= 1, "the fix must probe an embed to resolve the lazy downgrade"
    assert captured["semantic_vectors"] is False, (
        "degraded (hash) provider must pass semantic_vectors=False to ranking"
    )


def test_stable_semantic_provider_keeps_semantic_filter(tmp_path, monkeypatch):
    """Control: a genuinely-semantic provider still enables the semantic filter."""
    svc = _service(tmp_path, monkeypatch)
    svc.embedding_provider = _StableSemanticProvider()
    captured = _capture_semantic(svc, monkeypatch)

    svc.query_rows(query_text=TOKEN, retrieval_mode="hybrid", include_candidates=True, limit=10)

    assert captured["semantic_vectors"] is True
