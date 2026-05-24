"""Tests for the suspected-contradictions probe (v3.22, gbrain v0.32.6 port).

A deterministic fake embedding provider places claims at controlled angles so
pair similarities land predictably in/out of the sampling band; the LLM judge
is stubbed via _PROVIDERS so the real call_llm budget gate still runs.
"""
from __future__ import annotations

import json
import math

import pytest

from memorymaster import contradiction_probe, llm_provider
from memorymaster.config import reset_config
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


class _FakeProvider:
    """Maps a text marker -> 2-D unit vector at a given angle (radians)."""

    def __init__(self, angles: dict[str, float]):
        self.angles = angles

    def embed(self, text: str) -> list[float]:
        for marker, ang in self.angles.items():
            if marker in text:
                return [math.cos(ang), math.sin(ang)]
        return [0.0, 1.0]


# rate-limited (0 rad) vs no rate limit (0.78 rad ~= cos 0.71 -> in band)
# react (1.4 rad) is far from rate-limited (cos ~0.17 -> below band)
_ANGLES = {"rate-limited": 0.0, "no rate limit": 0.78, "react": 1.4}


@pytest.fixture
def env(tmp_path, monkeypatch):
    reset_config()
    for var in (
        "MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE",
        "MEMORYMASTER_LLM_FALLBACK_PROVIDER",
        "MEMORYMASTER_LLM_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")  # _model_key + stub target

    db = tmp_path / "mm.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    svc.ingest(text="the api is rate-limited at 100 requests per minute",
               citations=[CitationInput(source="t", locator="a")], confidence=0.4, source_agent="t")
    svc.ingest(text="there is no rate limit on the api endpoints whatsoever",
               citations=[CitationInput(source="t", locator="b")], confidence=0.9, source_agent="t")
    svc.ingest(text="the frontend uses react and vite for production builds",
               citations=[CitationInput(source="t", locator="c")], confidence=0.5, source_agent="t")

    holder = {"calls": 0}

    def _stub(prompt, text):
        holder["calls"] += 1
        low = text.lower()
        contradicts = "rate-limited" in low and "no rate limit" in low
        return json.dumps({"contradicts": contradicts, "severity": "high" if contradicts else "low",
                           "reason": "rate limit vs none" if contradicts else ""})

    monkeypatch.setitem(llm_provider._PROVIDERS, "google", _stub)
    return str(db), svc, holder, _FakeProvider(_ANGLES)


def test_probe_finds_planted_contradiction(env):
    db, svc, holder, prov = env
    stats = contradiction_probe.run_probe(db, svc, provider=prov)
    assert stats["contradictions"] == 1
    found = stats["found"][0]
    assert found["severity"] == "high"
    assert found["reason"]
    # CI must bracket the point rate.
    assert stats["rate_ci"][0] <= stats["rate"] <= stats["rate_ci"][1]


def test_verdict_cache_hit_on_rerun(env):
    db, svc, holder, prov = env
    first = contradiction_probe.run_probe(db, svc, provider=prov)
    calls_after_first = holder["calls"]
    assert first["llm_calls"] >= 1
    second = contradiction_probe.run_probe(db, svc, provider=prov)
    assert second["cache_hits"] >= 1
    assert second["llm_calls"] == 0  # all served from cache
    assert holder["calls"] == calls_after_first  # LLM never called again


def test_prefilter_skips_same_subject_predicate(env, monkeypatch):
    db, svc, holder, prov = env
    # Two claims with identical subject+predicate are the deterministic resolver's
    # job; the probe must not sample them as candidates.
    svc.ingest(text="deploy target is staging one", subject="deploy target", predicate="is",
               object_value="staging", citations=[CitationInput(source="t", locator="d")], source_agent="t")
    svc.ingest(text="deploy target is production two", subject="deploy target", predicate="is",
               object_value="production", citations=[CitationInput(source="t", locator="e")], source_agent="t")
    claims = svc.store.list_claims(limit=100, include_citations=False)
    # Force those two to identical embeddings so they'd be a pair if not pre-filtered.
    pairs = contradiction_probe.sample_candidate_pairs(
        claims, _FakeProvider({"deploy target": 0.1, "rate-limited": 0.0, "no rate limit": 0.78, "react": 1.4}),
        sim_low=0.6, sim_high=0.92,
    )
    pair_ids = {(a.subject, b.subject) for a, b, _ in pairs}
    assert ("deploy target", "deploy target") not in pair_ids


def test_apply_flags_lower_confidence_conflicted(env):
    db, svc, holder, prov = env
    stats = contradiction_probe.run_probe(db, svc, provider=prov, apply=True)
    assert stats["flagged_conflicted"] == 1
    flagged_id = stats["found"][0]["flag_candidate_id"]
    flagged = svc.store.get_claim(flagged_id)
    assert flagged.status == "conflicted"
    # The lower-confidence claim (0.4 rate-limited) is the one flagged.
    assert flagged.confidence == pytest.approx(0.4)


def test_no_contradiction_no_flag(env, monkeypatch):
    db, svc, holder, prov = env
    monkeypatch.setitem(llm_provider._PROVIDERS, "google",
                        lambda p, t: json.dumps({"contradicts": False, "severity": "low", "reason": ""}))
    stats = contradiction_probe.run_probe(db, svc, provider=prov, apply=True)
    assert stats["contradictions"] == 0
    assert stats["flagged_conflicted"] == 0


def test_budget_cap_aborts(env, monkeypatch):
    db, svc, holder, prov = env
    monkeypatch.setenv("MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE", "1")
    stats = contradiction_probe.run_probe(db, svc, provider=prov)
    assert stats["llm_calls"] == 1
    assert stats["aborted_reason"] == "calls_exhausted"


def test_rejects_postgres_dsn(env):
    _, svc, _, _ = env
    with pytest.raises(ValueError, match="SQLite-only"):
        contradiction_probe.run_probe("postgresql://localhost/mm", svc)


def test_wilson_interval_math():
    assert contradiction_probe.wilson_interval(0, 0) == (0.0, 0.0)
    lo, hi = contradiction_probe.wilson_interval(5, 10)
    assert lo < 0.5 < hi
    lo0, _ = contradiction_probe.wilson_interval(0, 10)
    assert lo0 == 0.0
    _, hi1 = contradiction_probe.wilson_interval(10, 10)
    assert hi1 == 1.0
