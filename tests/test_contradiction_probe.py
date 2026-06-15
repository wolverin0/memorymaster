"""Tests for the suspected-contradictions probe (v3.22, gbrain v0.32.6 port).

A deterministic fake embedding provider places claims at controlled angles so
pair similarities land predictably in/out of the sampling band; the LLM judge
is stubbed via _PROVIDERS so the real call_llm budget gate still runs.
"""
from __future__ import annotations

import json
import math

import pytest

from memorymaster.core import llm_provider
from memorymaster.govern import contradiction_probe
from memorymaster.core.config import reset_config
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


class _FakeProvider:
    """Maps a text marker -> 2-D unit vector at a given angle (radians).

    Duck-types EmbeddingProvider (model / is_semantic / embed) so it can be
    injected at ``svc.embedding_provider`` — the seam ``probe_for_claim``'s
    hybrid peer fetch resolves vectors through. Without this injection the
    tests depend on real sentence-transformers geometry: on machines without
    the model the degraded hash fallback scores the planted pair below the
    contradiction band and the probe silently finds zero pairs.
    """

    model = "fake-semantic-test"
    is_semantic = True
    dims = 2

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
    prov = _FakeProvider(_ANGLES)
    # Inject at the service seam so probe_for_claim's hybrid peer fetch uses
    # the planted geometry deterministically (no sentence-transformers needed).
    svc.embedding_provider = prov
    return str(db), svc, holder, prov


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


# ---------------------------------------------------------------------------
# T01: empty/invalid verdict severity must floor to "medium", not "low".
# WHY: a real contradiction that the judge tags with a blank/garbage severity
# must NOT be silently demoted to the least-actionable tier — the steward
# triages by severity, so "" -> "low" would bury genuine conflicts.
# ---------------------------------------------------------------------------


def test_coerce_severity_floors_blank_to_medium():
    assert contradiction_probe._coerce_severity("") == "medium"
    assert contradiction_probe._coerce_severity(None) == "medium"
    assert contradiction_probe._coerce_severity("urgent") == "medium"  # off-vocab
    assert contradiction_probe._coerce_severity("LOW") == "low"  # canonical, case-insensitive
    assert contradiction_probe._coerce_severity("high") == "high"


def _probe_one_claim(svc, prov_stub, target_text):
    """Run probe_for_claim for the claim whose text contains target_text."""
    claims = svc.store.list_claims(limit=100, include_citations=False)
    target = next(c for c in claims if target_text in c.text)
    return contradiction_probe.probe_for_claim(svc, target)


def test_probe_for_claim_blank_severity_becomes_medium(env, monkeypatch):
    db, svc, holder, prov = env
    # Judge says it contradicts but returns a blank severity. The reason floors
    # to "medium" rather than "low".
    monkeypatch.setitem(
        llm_provider._PROVIDERS, "google",
        lambda p, t: json.dumps({"contradicts": True, "severity": "", "reason": "x vs y"}),
    )
    result = _probe_one_claim(svc, prov, "rate-limited")
    contradiction_reasons = [r for r in result["reasons"]
                             if r["code"] == "contradiction_probe.semantic_pair"]
    assert contradiction_reasons, "expected at least one contradiction reason"
    # The intent: a blank judge severity must NEVER be silently downgraded to the
    # least-actionable "low" tier — it floors to "medium" so the steward triages it.
    assert all(r["severity"] == "medium" for r in contradiction_reasons)


# ---------------------------------------------------------------------------
# T02: run_probe(apply=True) must redact the judge reason before it lands in the
# events table — the same guard probe_for_claim already applies. WHY: an LLM
# judge reason can echo back sensitive text; the transition event is persisted
# and read by humans/wiki, so a secret there is a leak.
# ---------------------------------------------------------------------------


def test_safe_judge_reason_drops_leaky_text():
    # A clean reason passes through verbatim.
    assert contradiction_probe._safe_judge_reason("just a rate limit clash") == "just a rate limit clash"
    assert contradiction_probe._safe_judge_reason("") == ""
    # A reason carrying a secret is dropped (None) so it never reaches events.
    leaky = "token is sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ABCD"
    assert contradiction_probe._safe_judge_reason(leaky) is None


def test_run_probe_apply_redacts_event_reason(env, monkeypatch):
    db, svc, holder, prov = env
    secret = "AKIAIOSFODNN7EXAMPLE bearer sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    monkeypatch.setitem(
        llm_provider._PROVIDERS, "google",
        lambda p, t: json.dumps({
            "contradicts": ("rate-limited" in t.lower() and "no rate limit" in t.lower()),
            "severity": "high",
            "reason": secret,
        }),
    )
    stats = contradiction_probe.run_probe(db, svc, provider=prov, apply=True)
    assert stats["flagged_conflicted"] == 1
    flagged_id = stats["found"][0]["flag_candidate_id"]
    events = svc.store.list_events(claim_id=flagged_id)
    blob = " ".join(str(getattr(e, "reason", "")) for e in events)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in blob
    assert "AKIAIOSFODNN7EXAMPLE" not in blob


# ---------------------------------------------------------------------------
# probe-graph cluster: the returned ``found`` report payload must ALSO route the
# judge reason through the sensitivity guard — not just the events table. WHY:
# ``found`` is what callers print/log/surface to humans and the wiki; a raw
# verdict reason there leaks the exact secret the events-table guard already
# blocks. A flagged (leaky) reason is dropped to "" so the report never carries it.
# ---------------------------------------------------------------------------


def test_run_probe_found_payload_redacts_leaky_reason(env, monkeypatch):
    db, svc, holder, prov = env
    secret = "bearer sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 AKIAIOSFODNN7EXAMPLE"
    monkeypatch.setitem(
        llm_provider._PROVIDERS, "google",
        lambda p, t: json.dumps({
            "contradicts": ("rate-limited" in t.lower() and "no rate limit" in t.lower()),
            "severity": "high",
            "reason": secret,
        }),
    )
    stats = contradiction_probe.run_probe(db, svc, provider=prov)
    assert stats["contradictions"] == 1
    found = stats["found"][0]
    # The leaky reason is dropped from the report payload (blanked), never echoed.
    assert found["reason"] == ""
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in found["reason"]
    assert "AKIAIOSFODNN7EXAMPLE" not in found["reason"]


def test_run_probe_found_payload_keeps_clean_reason(env):
    # A clean (non-leaky) reason still flows into the report verbatim — the guard
    # only drops sensitive content, it does not blank every reason.
    db, svc, holder, prov = env
    stats = contradiction_probe.run_probe(db, svc, provider=prov)
    assert stats["found"][0]["reason"] == "rate limit vs none"


# ---------------------------------------------------------------------------
# probe-graph cluster: the verdict-cache connection must open with WAL +
# busy_timeout. WHY: the cache DB is the shared claims file that the steward,
# recall hook, and sync all write; without WAL/busy_timeout a concurrent writer
# turns the verdict INSERT into "database is locked", which re-pays the LLM cost
# and (per-claim path) counts as a probe error toward the circuit breaker.
# ---------------------------------------------------------------------------


def test_connect_verdict_cache_sets_wal_and_busy_timeout(env):
    db, svc, holder, prov = env
    conn = contradiction_probe._connect_verdict_cache(db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert str(mode).lower() == "wal"
        assert int(busy) >= 30000
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# T03: sample_candidate_pairs must stop the O(n^2) sweep early once `limit`
# in-band pairs are collected. WHY: on a large claim set the quadratic scan is
# the dominant cost; building thousands of pairs only to truncate to `limit`
# wastes work proportional to n^2.
# ---------------------------------------------------------------------------


def test_sample_pairs_early_break_bounds_cosine_calls(monkeypatch):
    from memorymaster.govern import contradiction_probe as cp

    class _Claim:
        def __init__(self, cid):
            self.id = cid
            self.status = "candidate"
            self.text = f"claim {cid}"
            self.subject = None
            self.predicate = None
            self.object_value = None
            self.supersedes_claim_id = None
            self.replaced_by_claim_id = None

    claims = [_Claim(i) for i in range(50)]

    class _AllInBand:
        def embed(self, text):
            return [1.0, 0.0]

    calls = {"n": 0}
    real_cosine = cp.cosine_similarity

    def _counting_cosine(a, b):
        calls["n"] += 1
        return 0.7  # always in [0.60, 0.92)

    monkeypatch.setattr(cp, "cosine_similarity", _counting_cosine)
    pairs = cp.sample_candidate_pairs(claims, _AllInBand(), limit=3)
    assert len(pairs) == 3
    # Without early-break this would be C(50,2) = 1225 cosine calls. With the
    # break we stop right after collecting the 3rd in-band pair.
    assert calls["n"] <= 5, f"expected early break, got {calls['n']} cosine calls"
    _ = real_cosine  # keep reference, silence lints


# ---------------------------------------------------------------------------
# T04: the verdict table DDL must be issued once per DB per process, not on
# every per-claim probe call. WHY: a steward cycle calls probe_for_claim once
# per claim; re-running CREATE TABLE IF NOT EXISTS + commit per claim is pure
# overhead. Behaviour (the table existing) is unchanged.
# ---------------------------------------------------------------------------


def test_ensure_verdict_table_runs_ddl_once_per_db(env, monkeypatch):
    db, svc, holder, prov = env
    cp = contradiction_probe
    cp._ensured_verdict_dbs.clear()  # fresh process-cache for this DB

    # sqlite3.Connection.execute is immutable (cannot be monkeypatched), so we
    # spy on the module's own DDL helper. It must short-circuit after the first
    # ensure for a given DB: only the FIRST per-claim call should run the
    # CREATE TABLE + commit; the rest hit the process-cache guard and return.
    real_ensure = cp._ensure_verdict_table
    ddl_runs = {"n": 0}

    def _spy_ensure(conn, *, db_key=None):
        before = db_key is not None and db_key in cp._ensured_verdict_dbs
        real_ensure(conn, db_key=db_key)
        if not before:  # actually issued the DDL this call
            ddl_runs["n"] += 1

    monkeypatch.setattr(cp, "_ensure_verdict_table", _spy_ensure)

    claims = svc.store.list_claims(limit=100, include_citations=False)
    target = next(c for c in claims if "rate-limited" in c.text)
    cp.probe_for_claim(svc, target)
    cp.probe_for_claim(svc, target)
    cp.probe_for_claim(svc, target)
    # The DDL is issued exactly once across the three per-claim calls.
    assert ddl_runs["n"] == 1, f"DDL issued {ddl_runs['n']} times, expected 1"
    # And the guard now records this DB as ensured.
    assert str(db) in cp._ensured_verdict_dbs
