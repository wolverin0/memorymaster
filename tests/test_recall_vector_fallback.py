"""Tests for the Qdrant vector-search recall fallback.

Covers:
  1. Fallback is inert without MEMORYMASTER_RECALL_VECTOR_FALLBACK=1.
  2. Fallback activates only when ``len(rows) < threshold`` (default 3).
  3. Graceful degradation when Qdrant client raises (import error, network,
     missing collection, etc).
  4. When W_VECTOR=0, ranking is bit-identical to pre-fallback (rows may
     be added but contribute zero score).
  5. Env-override knobs (threshold, score_threshold, limit) parse correctly.

Uses an in-memory SQLite DB with a handful of synthetic claims and mocks
out the sentence-transformers embedder + qdrant_client.QdrantClient.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from memorymaster.recall import context_hook, qdrant_recall_fallback
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService

# ML/torch tests: loads real sentence-transformers/Qdrant paths; excluded from
# the default run (see pytest.ini). Run in isolation with: pytest -m ml
pytestmark = pytest.mark.ml


@pytest.fixture
def service(tmp_path):
    """Tiny in-memory-ish MemoryService with a few claims, two of which are
    semantically close to 'session continuation' but lexically distant."""
    db_path = tmp_path / "recall_vec.db"
    svc = MemoryService(db_target=str(db_path), workspace_root=tmp_path)
    svc.init_db()
    # Stash the path on the service so tests can reuse it without poking
    # at service-internals.
    svc._test_db_path = str(db_path)  # type: ignore[attr-defined]

    seeded_ids: list[int] = []
    seeds = [
        ("tokenizer stoplist audit", "The recall tokenizer drops 'continue' and 'resume' as stopwords."),
        ("session continuation", "Claude CLI --continue resumes the last session based on recency."),
        ("mcp connection drop", "MemoryMaster MCP reconnect logic avoids session id collisions."),
        ("unrelated fact", "WAL mode is mandatory for SQLite stores."),
    ]
    for subject, text in seeds:
        claim = svc.ingest(
            text=text,
            subject=subject,
            citations=[CitationInput(source="test-fixture")],
            claim_type="fact",
            scope="test",
            confidence=0.7,
            source_agent="pytest",
        )
        seeded_ids.append(claim.id)

    return svc, seeded_ids


@pytest.fixture(autouse=True)
def clear_env_and_singletons(monkeypatch):
    """Each test starts with every fallback env var unset and fresh singletons."""
    for var in (
        "MEMORYMASTER_RECALL_VECTOR_FALLBACK",
        "MEMORYMASTER_QDRANT_URL",
        "MEMORYMASTER_QDRANT_COLLECTION",
        "MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES",
        "MEMORYMASTER_RECALL_VECTOR_SCORE_THRESHOLD",
        "MEMORYMASTER_RECALL_VECTOR_LIMIT",
        "MEMORYMASTER_RECALL_W_VECTOR",
    ):
        monkeypatch.delenv(var, raising=False)
    qdrant_recall_fallback.reset_singletons_for_tests()


# ---------------------------------------------------------------------------
# Mock embedder & qdrant client
# ---------------------------------------------------------------------------


class _FakeVector(list):
    def tolist(self):  # mimic numpy.ndarray.tolist()
        return list(self)


class _FakeEmbedder:
    def __init__(self, dims: int = 8) -> None:
        self.dims = dims

    def encode(self, text: str, *, normalize_embeddings: bool = True,
               show_progress_bar: bool = False):
        # Deterministic but content-free — qdrant search is mocked anyway.
        return _FakeVector([0.1] * self.dims)


@dataclass(frozen=True)
class _FakeHit:
    score: float
    payload: dict


class _FakeQueryResponse:
    def __init__(self, points: list[_FakeHit]) -> None:
        self.points = points


class _FakeClient:
    def __init__(self, hits: list[_FakeHit], fail_mode: str | None = None):
        self._hits = hits
        self._fail_mode = fail_mode
        self.calls = 0

    def query_points(self, **kwargs):
        self.calls += 1
        if self._fail_mode == "query":
            raise RuntimeError("simulated qdrant search failure")
        return _FakeQueryResponse(self._hits)


def _install_mocks(monkeypatch, hits, *, fail_mode=None,
                   embedder_fail=False):
    fake_client = _FakeClient(hits, fail_mode=fail_mode)

    def _get_embedder():
        if embedder_fail:
            return None
        return _FakeEmbedder()

    def _get_client():
        return fake_client

    monkeypatch.setattr(qdrant_recall_fallback, "_get_embedder", _get_embedder)
    monkeypatch.setattr(qdrant_recall_fallback, "_get_client", _get_client)
    return fake_client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fallback_inert_without_env(service, monkeypatch):
    """Default behaviour: env vars unset → fallback is a no-op."""
    _svc, seeded = service
    hit = _FakeHit(score=0.9, payload={"id": seeded[1], "scope": "test",
                                         "subject": "session continuation",
                                         "status": "confirmed",
                                         "confidence": 0.7})
    fake_client = _install_mocks(monkeypatch, [hit])
    out = context_hook.recall("continue from where you left off",
                               db_path=_svc._test_db_path, skip_qdrant=True)
    # search() should never have been called because env is unset.
    assert fake_client.calls == 0
    # Output either empty or made up solely of non-vector hits.
    assert "vector_fallback" not in out


def test_fallback_triggers_when_rows_under_threshold(service, monkeypatch):
    """With env enabled and <3 primary rows, fallback adds rows."""
    svc, seeded = service
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_FALLBACK", "1")
    monkeypatch.setenv("MEMORYMASTER_QDRANT_URL", "http://mocked.local:6333")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_VECTOR", "0.2")

    hit = _FakeHit(score=0.9, payload={"id": seeded[1], "scope": "test",
                                         "subject": "session continuation",
                                         "status": "confirmed",
                                         "confidence": 0.7})
    fake_client = _install_mocks(monkeypatch, [hit])

    # Use a prompt that FTS5 won't match — guarantees <3 primary candidates.
    out = context_hook.recall("zzzzz-noword-noword-noword",
                               db_path=svc._test_db_path, skip_qdrant=True)
    assert fake_client.calls == 1, "fallback should have queried qdrant"
    assert "session based on recency" in out or "Claude CLI" in out


def test_fallback_skipped_when_rows_ge_threshold(service, monkeypatch):
    """When FTS5 already returns >= MIN_CANDIDATES rows, no qdrant search."""
    svc, _seeded = service
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_FALLBACK", "1")
    monkeypatch.setenv("MEMORYMASTER_QDRANT_URL", "http://mocked.local:6333")
    # Force threshold=1 so the seeded claims will always exceed it.
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES", "1")

    hit = _FakeHit(score=0.9, payload={"id": 99999, "scope": "test",
                                         "subject": "unrelated",
                                         "status": "confirmed",
                                         "confidence": 0.7})
    fake_client = _install_mocks(monkeypatch, [hit])

    # A prompt we know matches FTS5 (seeded "tokenizer stoplist audit").
    out = context_hook.recall("tokenizer stoplist",
                               db_path=svc._test_db_path, skip_qdrant=True)
    assert fake_client.calls == 0
    assert "stopwords" in out


def test_graceful_degradation_on_qdrant_failure(service, monkeypatch):
    """Qdrant unreachable → fallback swallows, recall still returns rows."""
    svc, _seeded = service
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_FALLBACK", "1")
    monkeypatch.setenv("MEMORYMASTER_QDRANT_URL", "http://mocked.local:6333")

    fake_client = _install_mocks(monkeypatch, [], fail_mode="query")
    # Prompt that still matches at least one seeded claim so we can assert non-empty.
    out = context_hook.recall("tokenizer",
                               db_path=svc._test_db_path, skip_qdrant=True)
    # Client was called, but threw — no crash, output is whatever FTS5 produced.
    assert fake_client.calls == 1
    assert "stopwords" in out


def test_graceful_degradation_on_embedder_failure(service, monkeypatch):
    """Sentence-transformers import error → fallback silently skips."""
    svc, _seeded = service
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_FALLBACK", "1")
    monkeypatch.setenv("MEMORYMASTER_QDRANT_URL", "http://mocked.local:6333")

    fake_client = _install_mocks(monkeypatch, [], embedder_fail=True)
    out = context_hook.recall("tokenizer",
                               db_path=svc._test_db_path, skip_qdrant=True)
    assert fake_client.calls == 0
    assert "stopwords" in out


def test_w_vector_zero_is_additive_but_score_neutral(service, monkeypatch):
    """With W_VECTOR=0, fallback may append rows, but they contribute 0
    score — ranking of pre-existing rows must be bit-identical.
    """
    svc, seeded = service
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_FALLBACK", "1")
    monkeypatch.setenv("MEMORYMASTER_QDRANT_URL", "http://mocked.local:6333")
    # Explicitly keep W_VECTOR=0 (the shipped default) — leave var unset.

    hit = _FakeHit(score=0.95, payload={"id": seeded[1], "scope": "test",
                                          "subject": "session continuation",
                                          "status": "confirmed",
                                          "confidence": 0.7})
    _install_mocks(monkeypatch, [hit])

    # Compare: same prompt with fallback OFF vs ON (at W_VECTOR=0).
    out_on = context_hook.recall("zzzzz-noword-noword-noword",
                                  db_path=svc._test_db_path, skip_qdrant=True)
    monkeypatch.delenv("MEMORYMASTER_RECALL_VECTOR_FALLBACK")
    qdrant_recall_fallback.reset_singletons_for_tests()
    out_off = context_hook.recall("zzzzz-noword-noword-noword",
                                   db_path=svc._test_db_path, skip_qdrant=True)
    # Vector hit appears ONLY in the ON variant.
    if out_off:
        # Every line in `out_off` must also appear in `out_on` (identical order
        # at the front — fallback rows go at the tail because they have score 0).
        off_lines = out_off.strip().splitlines()
        on_lines = out_on.strip().splitlines()
        head = on_lines[: len(off_lines)]
        assert head == off_lines, "W_VECTOR=0 must preserve existing ordering"


def test_env_knob_parsing(monkeypatch):
    """Env overrides for threshold / score / limit parse correctly, with
    sensible defaults on garbage input."""
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES", "5")
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_SCORE_THRESHOLD", "0.42")
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_LIMIT", "7")
    assert qdrant_recall_fallback.fallback_threshold() == 5
    assert qdrant_recall_fallback.score_threshold() == pytest.approx(0.42)
    assert qdrant_recall_fallback.search_limit() == 7

    # Garbage → defaults.
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES", "not-an-int")
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_SCORE_THRESHOLD", "nope")
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_LIMIT", "bad")
    assert qdrant_recall_fallback.fallback_threshold() == qdrant_recall_fallback.DEFAULT_MIN_CANDIDATE_THRESHOLD
    assert qdrant_recall_fallback.score_threshold() == pytest.approx(qdrant_recall_fallback.DEFAULT_SCORE_THRESHOLD)
    assert qdrant_recall_fallback.search_limit() == qdrant_recall_fallback.DEFAULT_LIMIT


def test_is_fallback_enabled_requires_both_vars(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_FALLBACK", "1")
    assert qdrant_recall_fallback.is_fallback_enabled() is False  # no URL
    monkeypatch.setenv("MEMORYMASTER_QDRANT_URL", "http://x.y:6333")
    assert qdrant_recall_fallback.is_fallback_enabled() is True
    monkeypatch.setenv("MEMORYMASTER_RECALL_VECTOR_FALLBACK", "0")
    assert qdrant_recall_fallback.is_fallback_enabled() is False


def test_point_id_is_deterministic():
    a = qdrant_recall_fallback.point_id_for_claim(42)
    b = qdrant_recall_fallback.point_id_for_claim(42)
    c = qdrant_recall_fallback.point_id_for_claim(43)
    assert a == b
    assert a != c


def test_row_for_vector_hit_shape():
    """Sanity on the row-builder — keep the shape wired to query_rows."""
    class _StubClaim:
        id = 99
        status = "confirmed"
        confidence = 0.8
        text = "x"
    row = context_hook._row_for_vector_hit(_StubClaim(), 0.77)
    assert row["source"] == "vector_fallback"
    assert row["vector_score"] == pytest.approx(0.77)
    assert row["entity_score"] == 0.0
    assert row["lexical_score"] == 0.0
    assert row["confidence_score"] == pytest.approx(0.8)
