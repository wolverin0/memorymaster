"""Regression: a provider that silently falls back to hash must NOT keep
reporting itself as semantic.

WHY THIS MATTERS: query_rows captures ``is_semantic`` and passes it as
``semantic_vectors`` into ranking. The semantic_vectors=True path uses a
LENIENT filter that KEEPS any claim with vector_score >= 0.55 even with zero
lexical overlap. If the Gemini backend fails at runtime and silently downgrades
to hash embeddings (no semantic meaning), but ``is_semantic`` still reported
True, irrelevant claims would surface on that lenient path. After the fix the
provider exposes a ``degraded`` flag and ``is_semantic`` returns False once the
fallback fires — so a caller that re-reads it after embedding never runs the
lenient filter against non-semantic vectors. This test anchors on the
requirement (degraded provider is not semantic), not on the fallback mechanism.
"""
from __future__ import annotations

from memorymaster.recall.embeddings import EmbeddingProvider


class _BoomClient:
    class models:  # noqa: N801
        @staticmethod
        def embed_content(model, contents):  # noqa: ARG004
            raise RuntimeError("404 model deprecated")


def test_gemini_provider_is_semantic_until_it_fails():
    p = EmbeddingProvider(model="gemini:gemini-embedding-001", dims=768)
    # Before any call it is intended to be semantic.
    assert p.is_semantic is True
    assert p.degraded is False


def test_silent_downgrade_flips_is_semantic_false():
    p = EmbeddingProvider(model="gemini:gemini-embedding-001", dims=768)
    p._transformer = _BoomClient()

    vec = p.embed("some query text")

    # Still produces a usable vector (graceful degradation preserved)...
    assert len(vec) == 1536
    # ...but it must no longer claim to be semantic, so the caller's
    # semantic_vectors filter recomputes to the strict path.
    assert p.degraded is True
    assert p.is_semantic is False


def test_genuine_hash_provider_is_not_marked_degraded():
    # A provider that was always hash is non-semantic but NOT degraded — the
    # flag distinguishes "fell back" from "never tried".
    p = EmbeddingProvider(model="hash-v1", dims=1536)
    assert p.is_semantic is False
    assert p.degraded is False
