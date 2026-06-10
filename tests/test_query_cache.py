"""Tests for the correctness-safe query cache (v3.22, gbrain v0.40.3 port).

Opt-in via MEMORYMASTER_QUERY_CACHE=1. A cache hit must skip the ranking
recompute (proven by spying on rank_claim_rows); any claim write or config
change must invalidate (miss + recompute). The generation trigger must bump on
content writes but NOT on access recording (else the cache self-invalidates).
"""
from __future__ import annotations

import sqlite3

import pytest

import memorymaster.service as service_mod
from memorymaster.recall import query_cache
from memorymaster.config import reset_config
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService

_NO_VECTOR = lambda q, c: {}  # noqa: E731 — keeps tests fast/deterministic (no embed model)


@pytest.fixture
def env(tmp_path, monkeypatch):
    reset_config()
    monkeypatch.setenv("MEMORYMASTER_QUERY_CACHE", "1")
    monkeypatch.delenv("MEMORYMASTER_BOOST_FLOOR_RATIO", raising=False)
    db = tmp_path / "mm.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    for txt in ("alpha postgres database version sixteen",
                "beta react frontend vite build pipeline",
                "gamma redis caching layer ttl"):
        svc.ingest(text=txt, citations=[CitationInput(source="t", locator="l")],
                   confidence=0.5, source_agent="t")
    spy = {"n": 0}
    real = service_mod.rank_claim_rows

    def _spy(*a, **k):
        spy["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(service_mod, "rank_claim_rows", _spy)
    return str(db), svc, spy


def _q(svc, text="postgres database version"):
    return svc.query_rows(text, retrieval_mode="hybrid", include_candidates=True, vector_hook=_NO_VECTOR)


def test_cache_hit_skips_recompute(env):
    db, svc, spy = env
    first = _q(svc)
    assert spy["n"] == 1
    second = _q(svc)
    assert spy["n"] == 1  # served from cache — rank_claim_rows NOT called again
    assert [r["claim"].id for r in first] == [r["claim"].id for r in second]


def test_write_invalidates_cache(env):
    db, svc, spy = env
    _q(svc)
    assert spy["n"] == 1
    # A new claim bumps corpus_generation via the AFTER INSERT trigger.
    svc.ingest(text="delta new claim about postgres tuning",
               citations=[CitationInput(source="t", locator="l")], source_agent="t")
    _q(svc)
    assert spy["n"] == 2  # cache invalidated -> recomputed


def test_config_change_invalidates_cache(env, monkeypatch):
    db, svc, spy = env
    _q(svc)
    assert spy["n"] == 1
    # Changing a retrieval weight changes the config fingerprint -> new key -> miss.
    monkeypatch.setenv("MEMORYMASTER_BOOST_FLOOR_RATIO", "0.5")
    reset_config()
    _q(svc)
    assert spy["n"] == 2


def test_cache_disabled_by_default(tmp_path, monkeypatch):
    reset_config()
    monkeypatch.delenv("MEMORYMASTER_QUERY_CACHE", raising=False)
    db = tmp_path / "off.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    svc.ingest(text="alpha postgres database version",
               citations=[CitationInput(source="t", locator="l")], source_agent="t")
    svc.query_rows("postgres", retrieval_mode="hybrid", include_candidates=True, vector_hook=_NO_VECTOR)
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0]
    conn.close()
    assert n == 0  # nothing written when disabled


def test_generation_bumps_on_write_not_on_access(env):
    db, svc, spy = env
    conn = sqlite3.connect(db)
    g0 = query_cache.current_generation(conn)
    conn.close()

    svc.ingest(text="epsilon another postgres note",
               citations=[CitationInput(source="t", locator="l")], source_agent="t")
    conn = sqlite3.connect(db)
    g1 = query_cache.current_generation(conn)
    conn.close()
    assert g1 > g0  # INSERT bumped generation

    # A query records access (access_count/last_accessed) — excluded from the
    # UPDATE trigger, so the generation must NOT move.
    _q(svc)
    conn = sqlite3.connect(db)
    g2 = query_cache.current_generation(conn)
    conn.close()
    assert g2 == g1


def test_cache_hit_preserves_breakdown(env):
    db, svc, spy = env
    first = _q(svc)
    second = _q(svc)
    assert spy["n"] == 1
    # breakdown round-trips through the cache (None here since gate is off, but
    # the key must be present on rehydrated rows).
    assert all("breakdown" in r for r in second)
    assert [r["score"] for r in first] == [r["score"] for r in second]


def test_toctou_write_tags_compute_time_generation(env):
    """Regression for qc-generation-toctou: a result must be tagged with the
    generation captured BEFORE its corpus read, so a claim write that races in
    during compute correctly invalidates the cached entry instead of being
    served stale."""
    db, svc, spy = env
    g_before = query_cache.read_generation(db)
    # A concurrent claim write lands mid-compute and bumps the generation.
    svc.ingest(
        text="a racing new claim about postgres tuning",
        citations=[CitationInput(source="t", locator="l")],
        source_agent="t",
    )
    g_after = query_cache.read_generation(db)
    assert g_after > g_before

    # The service tags with the COMPUTE-TIME generation (g_before). A read at the
    # now-advanced generation must MISS the stale entry.
    query_cache.write(db, "k-toctou", [{"id": 1, "score": 1.0}], g_before)
    assert query_cache.read(db, "k-toctou") is None

    # A result genuinely computed at the current generation is served.
    query_cache.write(db, "k-toctou", [{"id": 1, "score": 1.0}], g_after)
    assert query_cache.read(db, "k-toctou") is not None


def test_stale_generation_rows_are_evicted_on_read_and_sweep(env):
    db, svc, _spy = env
    g_before = query_cache.read_generation(db)
    query_cache.write(db, "k-stale-read", [{"id": 1, "score": 1.0}], g_before)

    svc.ingest(
        text="zeta new claim bumps cache generation",
        citations=[CitationInput(source="t", locator="l")],
        source_agent="t",
    )

    conn = sqlite3.connect(db)
    before = conn.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0]
    conn.close()

    assert query_cache.read(db, "k-stale-read") is None

    conn = sqlite3.connect(db)
    after = conn.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0]
    conn.close()
    assert before == 1
    assert after == 0

    g_after = query_cache.read_generation(db)
    query_cache.write(db, "k-stale-a", [{"id": 1, "score": 1.0}], g_before)
    query_cache.write(db, "k-stale-b", [{"id": 2, "score": 0.5}], g_before)
    query_cache.write(db, "k-current", [{"id": 3, "score": 0.25}], g_after)

    query_cache.evict_stale(db)

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT cache_key, generation FROM query_cache ORDER BY cache_key"
    ).fetchall()
    conn.close()

    assert rows == [("k-current", g_after)]
