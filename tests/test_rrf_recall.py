"""Opt-in ``rrf`` retrieval mode: RRF fusion, rerank fallback, mode switch, authorization.

The real cross-encoder is never loaded (torch segfaults the full run on this box):
scoring goes through an injected ``score_fn`` or a poisoned loader.
"""
from __future__ import annotations

from datetime import date

import pytest

from memorymaster.core.models import CitationInput, Claim
from memorymaster.core.service import MemoryService
from memorymaster.recall import local_rerank, rrf_recall
from memorymaster.recall.planner import RetrievalRequest, build_retrieval_plan
from memorymaster.recall.recall_fusion import RRF_K_DEFAULT


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    def _boom():
        raise AssertionError("real CrossEncoder load attempted in tests")

    monkeypatch.setattr(local_rerank, "_get_model", _boom)
    for name in (rrf_recall.MODE_ENV, rrf_recall.RERANK_ENV, rrf_recall.TOP_N_ENV, rrf_recall.TOKEN_BUDGET_ENV):
        monkeypatch.delenv(name, raising=False)


def _c(cid: int, text: str = "") -> Claim:
    return Claim(
        id=cid, text=text or f"claim {cid}", idempotency_key=None, normalized_text=None,
        claim_type=None, subject=None, predicate=None, object_value=None, scope="project:t",
        volatility="medium", status="confirmed", confidence=0.5, pinned=False,
        supersedes_claim_id=None, replaced_by_claim_id=None, created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00", last_validated_at=None, archived_at=None,
    )


# --- fusion -------------------------------------------------------------------

def test_fuse_streams_sums_reciprocal_ranks_with_k60():
    a, b, c = _c(1), _c(2), _c(3)
    hits = rrf_recall.fuse_streams({"lexical": [a, b], "dense": [b, c]})
    by_id = {h.claim.id: h for h in hits}
    assert RRF_K_DEFAULT == 60
    assert by_id[2].rrf_score == pytest.approx(1 / 62 + 1 / 61)
    assert by_id[1].rrf_score == pytest.approx(1 / 61)
    assert by_id[3].rrf_score == pytest.approx(1 / 62)
    assert [h.claim.id for h in hits] == [2, 1, 3]  # in two streams beats rank 1 in one
    assert by_id[2].ranks == {"lexical": 2, "dense": 1}


def test_fuse_streams_ignores_empty_streams_and_counts_repeats_once():
    a = _c(1)
    hits = rrf_recall.fuse_streams({"lexical": [a, a], "dense": [], "time": [a]})
    assert len(hits) == 1
    assert hits[0].rrf_score == pytest.approx(1 / 61 + 1 / 61)
    assert hits[0].ranks == {"lexical": 1, "time": 1}


def test_fuse_streams_ties_are_deterministic():
    hits = rrf_recall.fuse_streams({"x": [_c(5)], "y": [_c(3)]})
    assert [h.claim.id for h in hits] == [3, 5]  # equal score and rank -> lower id first


# --- rerank / budget ------------------------------------------------------------

def test_rerank_head_reorders_only_the_head():
    hits = rrf_recall.fuse_streams({"s": [_c(1), _c(2), _c(3), _c(4)]})
    out = rrf_recall.rerank_head("q", hits, top_n=3, score_fn=lambda q, t: [0.1, 0.9, 0.5])
    assert [h.claim.id for h in out] == [2, 3, 1, 4]
    assert out[0].rerank_score == pytest.approx(0.9)
    assert out[3].rerank_score is None


@pytest.mark.parametrize("bad", [None, [0.1]])
def test_rerank_failure_keeps_fused_order(bad):
    hits = rrf_recall.fuse_streams({"s": [_c(1), _c(2), _c(3)]})
    out = rrf_recall.rerank_head("q", hits, top_n=3, score_fn=lambda q, t: bad)
    assert [h.claim.id for h in out] == [1, 2, 3]
    assert all(h.rerank_score is None for h in out)


def test_trim_to_budget_keeps_at_least_one_and_respects_limit():
    hits = rrf_recall.fuse_streams({"s": [_c(i, "x" * 40) for i in range(1, 6)]})  # ~10 tokens each
    assert len(rrf_recall.trim_to_budget(hits, 4, 0)) == 4
    assert len(rrf_recall.trim_to_budget(hits, 5, 25)) == 2
    assert len(rrf_recall.trim_to_budget(hits, 5, 1)) == 1


def test_rrf_rank_uses_rerank_then_limit(monkeypatch):
    streams = {"lexical": [_c(1), _c(2), _c(3)]}
    out = rrf_recall.rrf_rank("q", streams, {}, limit=2, score_fn=lambda q, t: [0.0, 0.2, 0.9])
    assert [h.claim.id for h in out] == [3, 2]
    monkeypatch.setenv(rrf_recall.RERANK_ENV, "0")
    out = rrf_recall.rrf_rank("q", streams, {}, limit=2, score_fn=lambda q, t: [0.0, 0.2, 0.9])
    assert [h.claim.id for h in out] == [1, 2]


def test_rrf_rank_demotes_pending_supersession():
    streams = {"lexical": [_c(1), _c(2), _c(3)]}
    out = rrf_recall.rrf_rank("q", streams, {}, limit=3, score_fn=lambda q, t: None,
                              pending_supersession_ids=frozenset({1}))
    assert [h.claim.id for h in out] == [2, 3, 1]


# --- time range -----------------------------------------------------------------

TODAY = date(2026, 10, 27)


@pytest.mark.parametrize("query,expected", [
    ("what happened on 2026-10-05", ("2026-10-05", "2026-10-06")),
    ("decisions in 2026-02", ("2026-02-01", "2026-03-01")),
    ("changes in 2026-12", ("2026-12-01", "2027-01-01")),
    ("what did we do yesterday", ("2026-10-26", "2026-10-27")),
    ("que hicimos hoy", ("2026-10-27", "2026-10-28")),
    ("bugs in the last 3 days", ("2026-10-24", "2026-10-28")),
    ("la semana pasada", ("2026-10-20", "2026-10-27")),
    ("no temporal words here", None),
    ("bad date 2026-13-45", None),
])
def test_parse_time_range(query, expected):
    assert rrf_recall.parse_time_range(query, today=TODAY) == expected


# --- mode switch ----------------------------------------------------------------

def test_resolve_mode_default_unchanged_and_env_opt_in(monkeypatch):
    assert rrf_recall.resolve_mode("legacy") == "legacy"
    monkeypatch.setenv(rrf_recall.MODE_ENV, "rrf")
    assert rrf_recall.resolve_mode("legacy") == "rrf"
    assert rrf_recall.resolve_mode("hybrid") == "hybrid"  # explicit modes are never overridden
    monkeypatch.setenv(rrf_recall.MODE_ENV, "hybrid")
    assert rrf_recall.resolve_mode("legacy") == "legacy"  # only the new token opts in


def test_planner_accepts_rrf_mode():
    plan = build_retrieval_plan(RetrievalRequest(query_text="x", retrieval_mode="rrf"))
    assert plan.effective_mode == "rrf"


def _service(tmp_path, tenant="tenant-a") -> MemoryService:
    service = MemoryService(tmp_path / "rrf.db", workspace_root=tmp_path, tenant_id=tenant)
    service.init_db()
    return service


def _make(service, text, **kwargs):
    params = {"citations": [CitationInput(source="synthetic-test")], "confidence": 0.8,
              "tenant_id": "tenant-a", "scope": "project:allowed"}
    params.update(kwargs)
    return service.store.create_claim(text=text, **params)


class _Provider:
    model = "test-rrf-v1"
    is_semantic = True

    def embed(self, text):
        return [1.0, 0.0]

    def embed_batch(self, texts):
        return [[1.0, 0.0] if "semantic" in t else [0.0, 1.0] for t in texts]


def test_query_rows_default_never_enters_rrf_and_env_switches_it(tmp_path, monkeypatch):
    service = _service(tmp_path)
    _make(service, "satellite relay authenticates with signed tokens")
    calls: list[str] = []
    real = rrf_recall.collect_streams

    def spy(*a, **k):
        calls.append("rrf")
        return real(*a, **k)

    monkeypatch.setattr(rrf_recall, "collect_streams", spy)
    monkeypatch.setenv(rrf_recall.RERANK_ENV, "0")
    service.embedding_provider = _Provider()

    service.query_rows("satellite relay", record_accesses=False, include_candidates=True)
    service.query_rows("satellite relay", retrieval_mode="hybrid", record_accesses=False, include_candidates=True)
    assert calls == []

    monkeypatch.setenv(rrf_recall.MODE_ENV, "rrf")
    rows = service.query_rows("satellite relay", record_accesses=False, include_candidates=True)
    assert calls == ["rrf"]
    assert [r["claim"].text for r in rows] == ["satellite relay authenticates with signed tokens"]
    assert rows[0]["breakdown"]["mode"] == "rrf"
    assert "lexical" in rows[0]["breakdown"]["ranks"]

    service.query_rows("satellite relay", retrieval_mode="rrf", record_accesses=False, include_candidates=True)
    assert calls == ["rrf", "rrf"]


def test_rrf_mode_streams_come_from_injected_retrievers(tmp_path, monkeypatch):
    service = _service(tmp_path)
    a = _make(service, "alpha one")
    b = _make(service, "beta two")
    monkeypatch.setenv(rrf_recall.RERANK_ENV, "0")
    monkeypatch.setattr(
        rrf_recall, "collect_streams",
        lambda *a_, **k: ({"lexical": [a], "dense": [b, a]}, {b.id: 0.9}),
    )
    rows = service.query_rows("anything", retrieval_mode="rrf", record_accesses=False, include_candidates=True)
    assert [r["claim"].id for r in rows] == [a.id, b.id]
    assert rows[1]["vector_score"] == pytest.approx(0.9)


# --- authorization (real SQLite) --------------------------------------------------

def test_rrf_mode_never_returns_unauthorized_claims_from_any_stream(tmp_path, monkeypatch):
    """lexical + dense + time streams all hit the same tenant/scope/visibility/sensitivity gate."""
    service = _service(tmp_path)
    service.embedding_provider = _Provider()
    monkeypatch.setenv(rrf_recall.RERANK_ENV, "0")
    ok = _make(service, "semantic relay certificate rotation ok")
    blocked = {
        "tenant": _make(service, "semantic relay certificate BLOCK-TENANT", tenant_id="tenant-b"),
        "scope": _make(service, "semantic relay certificate BLOCK-SCOPE", scope="project:other"),
        "private": _make(service, "semantic relay certificate BLOCK-PRIVATE",
                         source_agent="other-agent", visibility="private"),
        "sensitive": _make(service, "semantic relay BLOCK-SENSITIVE AKIAIOSFODNN7EXAMPLE"),
        "expired": _make(service, "semantic relay certificate BLOCK-EXPIRED",
                         valid_until="2000-01-01T00:00:00Z"),
    }
    # "today" also activates the time stream: every claim above was created today.
    rows = service.query_rows(
        "semantic relay certificate today", retrieval_mode="rrf", limit=20,
        scope_allowlist=["project:allowed"], requesting_agent="reader-agent",
        record_accesses=False, include_candidates=True,
    )
    ids = {r["claim"].id for r in rows}
    assert ok.id in ids
    assert not ids & {c.id for c in blocked.values()}
    assert {"time", "dense", "lexical"} <= {s for r in rows for s in r["breakdown"]["ranks"]}


def test_rrf_stream_failure_degrades_without_failing_recall(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.embedding_provider = _Provider()
    monkeypatch.setenv(rrf_recall.RERANK_ENV, "0")
    _make(service, "semantic relay certificate rotation")

    def boom(*a, **k):
        raise RuntimeError("entity table gone")

    monkeypatch.setattr(rrf_recall, "entity_stream", boom)
    rows = service.query_rows("semantic relay certificate", retrieval_mode="rrf", record_accesses=False, include_candidates=True)
    assert len(rows) == 1


def test_entity_stream_proposes_linked_claims_but_authorization_still_applies(tmp_path, monkeypatch):
    import sqlite3

    from memorymaster.knowledge.entity_registry import ensure_entity_schema, resolve_or_create

    service = _service(tmp_path)
    service.embedding_provider = _Provider()
    monkeypatch.setenv(rrf_recall.RERANK_ENV, "0")
    ok = _make(service, "unrelated wording about billing")
    private = _make(service, "other unrelated wording", source_agent="other-agent", visibility="private")
    wrong_scope = _make(service, "third unrelated wording", scope="project:other")
    conn = sqlite3.connect(tmp_path / "rrf.db")
    try:
        ensure_entity_schema(conn)
        entity_id = resolve_or_create(conn, "Zorbulax")
        conn.executemany(
            "INSERT INTO claim_entity_links (claim_id, entity_id) VALUES (?, ?)",
            [(c.id, entity_id) for c in (ok, private, wrong_scope)],
        )
        conn.commit()
    finally:
        conn.close()
    rows = service.query_rows(
        "zorbulax", retrieval_mode="rrf", scope_allowlist=["project:allowed"],
        requesting_agent="reader-agent", record_accesses=False, include_candidates=True,
    )
    assert [r["claim"].id for r in rows] == [ok.id]
    assert "entity" in rows[0]["breakdown"]["ranks"]
