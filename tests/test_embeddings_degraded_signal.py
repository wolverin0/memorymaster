"""Regression: a semantic backend that fails must raise, never turn into hash vectors.

WHY THIS MATTERS: the old Gemini provider caught any error, flipped its model to
``hash-v1`` and returned hash vectors. The stores then wrote those inert rows, and
``query_rows`` could not tell that the semantic backend had failed. Now a failed call
(HTTP error, timeout, daily quota) raises ``EncoderUnavailable``; ``query_rows`` ranks
lexically on it and steward jobs skip. These tests anchor on that requirement: no
failure path ever produces a hash vector or changes the provider's model.
"""
from __future__ import annotations

import pytest

from memorymaster.recall.embeddings import (
    EmbeddingProvider,
    EncoderUnavailable,
    UnavailableProvider,
)


class _BoomClient:
    class models:  # noqa: N801
        @staticmethod
        def embed_content(model, contents):  # noqa: ARG004
            raise RuntimeError("404 model deprecated")


def test_gemini_provider_is_semantic():
    p = EmbeddingProvider(model="gemini:gemini-embedding-001", dims=768)
    assert p.is_semantic is True


def test_gemini_single_failure_raises_and_never_becomes_hash():
    p = EmbeddingProvider(model="gemini:gemini-embedding-001", dims=768)
    p._transformer = _BoomClient()

    with pytest.raises(EncoderUnavailable):
        p.embed("some query text")

    assert p.model == "gemini:gemini-embedding-001"
    assert p.dims == 768
    assert p.is_semantic is True  # still the semantic backend; the call failed, nothing was faked


def test_gemini_batch_failure_raises_and_never_becomes_hash():
    p = EmbeddingProvider(model="gemini:gemini-embedding-001", dims=768)
    p._transformer = _BoomClient()

    with pytest.raises(EncoderUnavailable):
        p.embed_batch(["a", "b"])

    assert p.model == "gemini:gemini-embedding-001"


def test_gemini_quota_exhaustion_raises_encoder_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORYMASTER_USAGE_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("MEMORYMASTER_MAX_EMBEDDING_ITEMS_PER_DAY", "3")
    calls: list[int] = []

    class _Client:
        class models:  # noqa: N801
            @staticmethod
            def embed_content(model, contents):  # noqa: ARG004
                calls.append(len(contents))
                raise AssertionError("the quota must stop the request before it is sent")

    p = EmbeddingProvider(model="gemini:gemini-embedding-001", dims=768)
    p._transformer = _Client()

    with pytest.raises(EncoderUnavailable):
        p.embed_batch(["a", "b", "c", "d"])

    assert calls == []
    assert p.model == "gemini:gemini-embedding-001"


def test_genuine_hash_provider_still_works_and_is_not_semantic():
    p = EmbeddingProvider(model="hash-v1", dims=64)
    assert p.is_semantic is False
    assert len(p.embed("hello world")) == 64


def test_unavailable_provider_raises_for_embed_and_batch():
    p = UnavailableProvider(reason="because")
    assert p.is_semantic is False
    assert not p.model.startswith("hash")
    with pytest.raises(EncoderUnavailable, match="because"):
        p.embed("x")
    with pytest.raises(EncoderUnavailable, match="because"):
        p.embed_batch(["x"])
