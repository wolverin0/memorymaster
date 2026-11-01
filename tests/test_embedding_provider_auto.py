"""S1: ``auto`` never egresses to Google and never produces hash vectors.

Before: with sentence-transformers unavailable and ``GEMINI_API_KEY`` set, ``auto`` picked
Gemini and sent raw claim text and queries to Google (no redaction, no cap); a failed call
silently became hash-v1; ``upsert_embeddings`` sent every stale text in one request (HTTP 400
"at most 100 requests"). Only an explicit ``MEMORYMASTER_EMBEDDING_PROVIDER=gemini`` may egress,
and only an explicit ``hash`` gives hash vectors.
"""
from __future__ import annotations

import socket
import urllib.request
from types import SimpleNamespace

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import embeddings
from memorymaster.recall.embeddings import (
    EmbeddingProvider,
    EncoderUnavailable,
    UnavailableProvider,
    create_best_provider,
)
from memorymaster.stores.storage import SQLiteStore


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "MEMORYMASTER_EMBEDDING_PROVIDER",
        "MEMORYMASTER_USAGE_LEDGER_DB",
        "MEMORYMASTER_MAX_EMBEDDING_ITEMS_PER_DAY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _no_sentence_transformers(monkeypatch):
    def _missing(*args, **kwargs):
        raise ImportError("sentence-transformers is not installed")

    monkeypatch.setattr(embeddings, "create_semantic_provider", _missing)


def _forbid_network(monkeypatch):
    """Any HTTP/socket/genai client use fails the test."""

    def _boom(*args, **kwargs):
        raise AssertionError("network egress attempted")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    genai = pytest.importorskip("google.genai")
    monkeypatch.setattr(genai, "Client", _boom)
    monkeypatch.setattr(embeddings, "_load_gemini_client", _boom)


def test_auto_with_key_and_no_sentence_transformers_makes_zero_http_calls(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    _no_sentence_transformers(monkeypatch)
    _forbid_network(monkeypatch)

    provider = create_best_provider()

    assert isinstance(provider, UnavailableProvider)
    assert provider.is_semantic is False
    assert not provider.model.startswith(("hash", "gemini"))
    with pytest.raises(EncoderUnavailable):
        provider.embed("raw claim text that must stay local")
    with pytest.raises(EncoderUnavailable):
        provider.embed_batch(["raw claim text that must stay local"])


def test_auto_when_sentence_transformers_fails_to_load_is_unavailable_not_gemini_or_hash(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setattr(
        embeddings, "create_semantic_provider", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    _forbid_network(monkeypatch)
    assert isinstance(create_best_provider(), UnavailableProvider)


@pytest.mark.parametrize("value", ["sentence-transformers", "nonsense"])
def test_other_values_never_select_gemini_or_hash(monkeypatch, value):
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", value)
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    _no_sentence_transformers(monkeypatch)
    _forbid_network(monkeypatch)
    assert isinstance(create_best_provider(), UnavailableProvider)


def test_only_the_explicit_hash_value_gives_hash_vectors(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", "hash")
    provider = create_best_provider()
    assert provider.model == "hash-v1"
    assert len(provider.embed("x")) == provider.dims


def test_explicit_gemini_is_the_only_way_to_select_gemini(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    genai = pytest.importorskip("google.genai")
    monkeypatch.setattr(genai, "Client", lambda api_key: SimpleNamespace(api_key=api_key))
    provider = create_best_provider()
    assert provider.model.startswith("gemini:")


def test_explicit_gemini_without_a_key_is_unavailable_not_hash(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert isinstance(create_best_provider(), UnavailableProvider)


# --- 250 stale claims -> 3 requests of at most 100 ---------------------------------------


class _FakeGeminiClient:
    """Mimics the real API limit: more than 100 contents in one call is an HTTP 400."""

    def __init__(self) -> None:
        self.request_sizes: list[int] = []
        self.models = SimpleNamespace(embed_content=self._embed_content)

    def _embed_content(self, model, contents):  # noqa: ARG002
        texts = [contents] if isinstance(contents, str) else list(contents)
        self.request_sizes.append(len(texts))
        if len(texts) > 100:
            raise RuntimeError("400 INVALID_ARGUMENT: at most 100 requests can be in one batch")
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, float(i % 7), 0.5]) for i in range(len(texts))])


def _gemini_provider(client) -> EmbeddingProvider:
    provider = EmbeddingProvider(model="gemini:gemini-embedding-001", dims=3)
    provider._transformer = client
    return provider


def test_250_stale_claims_are_sent_as_three_requests_of_at_most_100(tmp_path):
    store = SQLiteStore(tmp_path / "chunk.db")
    store.init_db()
    claims = [
        store.create_claim(text=f"claim number {i}", citations=[CitationInput(source="test")])
        for i in range(250)
    ]
    client = _FakeGeminiClient()
    provider = _gemini_provider(client)

    assert store.upsert_embeddings(claims, provider) == 250

    assert client.request_sizes == [100, 100, 50]
    assert provider.model == "gemini:gemini-embedding-001"
    with store.connect() as conn:
        models = {row[0] for row in conn.execute("SELECT DISTINCT model FROM claim_embeddings")}
        rows = conn.execute("SELECT COUNT(*) FROM claim_embeddings").fetchone()[0]
    assert models == {"gemini:gemini-embedding-001"}
    assert rows == 250


def test_a_failing_gemini_call_writes_no_hash_rows(tmp_path):
    store = SQLiteStore(tmp_path / "fail.db")
    store.init_db()
    claims = [store.create_claim(text=f"claim {i}", citations=[CitationInput(source="test")]) for i in range(3)]
    provider = EmbeddingProvider(model="gemini:gemini-embedding-001", dims=3)
    provider._transformer = SimpleNamespace(
        models=SimpleNamespace(embed_content=lambda model, contents: (_ for _ in ()).throw(RuntimeError("503")))
    )

    with pytest.raises(EncoderUnavailable):
        store.upsert_embeddings(claims, provider)

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM claim_embeddings").fetchone()[0] == 0
    assert provider.model == "gemini:gemini-embedding-001"


# --- (d) the query path stays lexical when the provider is unavailable -----------------


def test_query_rows_still_returns_lexical_results_when_the_provider_is_unavailable(tmp_path, caplog):
    svc = MemoryService(tmp_path / "m.db", workspace_root=tmp_path)
    svc.init_db()
    claim = svc.store.create_claim(
        text="orbital relay handshake", citations=[CitationInput(source="synthetic-test")], confidence=0.9,
    )
    with svc.store.connect() as conn:
        conn.execute("UPDATE claims SET status='confirmed' WHERE id=?", (claim.id,))
        conn.commit()
    svc.embedding_provider = UnavailableProvider(reason="sentence-transformers is not installed")

    with caplog.at_level("WARNING", logger="memorymaster.core.service"):
        rows = svc.query_rows("orbital relay", retrieval_mode="hybrid", limit=5)

    assert [row["claim"].id for row in rows] == [claim.id]
    assert "ranking lexically" in caplog.text
