"""Steward-cycle integration for the contradiction probe (v3.23).

Verifies that when two semantically-contradicting claims with DIFFERENT
subject+predicate keys exist (so the deterministic conflict_resolver doesn't
catch them), the contradiction_probe phase elevates the steward's decision to
``conflicted`` and emits a paste-ready ``policy_decision`` proposal.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from memorymaster.core import llm_provider
from memorymaster.core.config import reset_config
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.steward import run_steward


def _case_db(prefix: str) -> Path:
    Path(".tmp_cases").mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def _case_workspace(prefix: str) -> Path:
    Path(".tmp_pytest").mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=".tmp_pytest"))


def _force_status(db: Path, claim_id: int, status: str, updated_at: str) -> None:
    con = sqlite3.connect(str(db))
    con.execute("DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_update")
    con.execute("DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_insert")
    con.execute("DROP INDEX IF EXISTS idx_claims_confirmed_tuple_unique")
    con.execute("DROP INDEX IF EXISTS idx_claims_public_confirmed_tuple_unique")
    con.execute(
        "DROP INDEX IF EXISTS idx_claims_nonpublic_principal_confirmed_tuple_unique"
    )
    con.execute(
        "UPDATE claims SET status=?, updated_at=?, last_validated_at=? WHERE id=?",
        (status, updated_at, updated_at, claim_id),
    )
    con.commit()
    con.close()


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    reset_config()
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
    monkeypatch.delenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", raising=False)
    monkeypatch.delenv("MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE", raising=False)
    yield
    reset_config()


class _FakeEmbeddings:
    """Deterministic 2-D embeddings for the planted contradiction pair.

    Duck-types EmbeddingProvider and is injected at ``service.embedding_provider``
    so the probe's hybrid peer fetch scores the planted pair inside the
    contradiction band (cos 0.78 rad ~= 0.71) on ANY machine. Without this the
    test depends on real sentence-transformers geometry; in environments
    without the model, the degraded hash fallback scores the pair below the
    band and the probe finds zero pairs (decision stays 'stale').
    """

    model = "fake-semantic-test"
    is_semantic = True
    dims = 2

    def embed(self, text: str) -> list[float]:
        import math
        low = text.lower()
        if "rate-limited" in low:
            return [1.0, 0.0]
        if "no rate limit" in low:
            return [math.cos(0.78), math.sin(0.78)]
        return [0.0, 1.0]


def _stub_judge(prompt: str, body: str) -> str:
    """LLM stub: contradicts iff both planted markers appear in the prompt body."""
    low = body.lower()
    contradicts = "rate-limited" in low and "no rate limit" in low
    return json.dumps({
        "contradicts": contradicts,
        "severity": "high" if contradicts else "low",
        "reason": "rate limit vs no rate limit" if contradicts else "",
    })


def test_contradiction_probe_elevates_to_conflicted_proposal(monkeypatch) -> None:
    db = _case_db("steward-contradiction")
    workspace = _case_workspace("steward-contradiction-ws")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    service.embedding_provider = _FakeEmbeddings()
    monkeypatch.setitem(llm_provider._PROVIDERS, "google", _stub_judge)

    # Two claims that contradict semantically but have DIFFERENT subject+predicate
    # keys, so the deterministic conflict_resolver does NOT catch them.
    rate_limited = service.ingest(
        text="The public API is rate-limited at 100 requests per minute per key.",
        citations=[CitationInput(source="docs", locator="rate-limit.md", excerpt="100/min")],
        subject="api rate limit", predicate="value", object_value="100 per minute",
        confidence=0.4,
    )
    no_limit = service.ingest(
        text="There is no rate limit on the public API endpoints whatsoever.",
        citations=[CitationInput(source="docs", locator="api-overview.md", excerpt="unlimited")],
        subject="api throttling", predicate="state", object_value="unlimited",
        confidence=0.9,
    )
    _force_status(db, rate_limited.id, "confirmed", "2026-01-01T00:00:00+00:00")
    _force_status(db, no_limit.id, "confirmed", "2026-02-01T00:00:00+00:00")

    artifact = workspace / "artifacts" / "steward_report.json"
    report = run_steward(
        service, mode="manual", max_cycles=1, max_claims=10, max_proposals=10,
        max_probe_files=5, apply=False, artifact_path=artifact,
        # Disable other heavy/noisy probes to keep the test focused + fast.
        enable_semantic_probe=False, enable_tool_probe=False,
        enable_contradiction_probe=True,
    )

    cycle = report["cycles"][0]
    by_id = {int(d["claim_id"]): d for d in cycle["decisions"]}
    # The lower-confidence claim should be flagged conflicted by the probe.
    lower_id = rate_limited.id  # 0.4 vs 0.9
    assert lower_id in by_id, f"steward did not consider lower-confidence claim; saw {list(by_id)}"
    decision = by_id[lower_id]
    assert decision["decision"] == "conflicted", (
        f"expected conflicted decision; got {decision['decision']!r}. reasons={decision.get('reasons')}"
    )
    reason_codes = {r["code"] for r in decision["reasons"]}
    assert "contradiction_probe.semantic_pair" in reason_codes

    # A policy_decision event must be persisted for the proposal flow.
    events = service.list_events(claim_id=lower_id, event_type="policy_decision", limit=10)
    assert events, "no policy_decision event recorded"
    assert any((e.details or "").startswith("steward_proposal:") for e in events)


def test_disable_flag_skips_contradiction_probe(monkeypatch) -> None:
    """--disable-contradiction-probe path: even with the planted pair, no probe
    runs, so no contradiction reason and no conflicted decision."""
    db = _case_db("steward-contra-off")
    workspace = _case_workspace("steward-contra-off-ws")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    calls = {"n": 0}

    def _counting_judge(p, b):
        calls["n"] += 1
        return _stub_judge(p, b)

    monkeypatch.setitem(llm_provider._PROVIDERS, "google", _counting_judge)

    a = service.ingest(
        text="The public API is rate-limited at 100 requests per minute per key.",
        citations=[CitationInput(source="d", locator="a")],
        subject="api rate limit", predicate="value", object_value="100",
        confidence=0.4,
    )
    b = service.ingest(
        text="There is no rate limit on the public API endpoints whatsoever.",
        citations=[CitationInput(source="d", locator="b")],
        subject="api throttling", predicate="state", object_value="unlimited",
        confidence=0.9,
    )
    _force_status(db, a.id, "confirmed", "2026-01-01T00:00:00+00:00")
    _force_status(db, b.id, "confirmed", "2026-02-01T00:00:00+00:00")

    report = run_steward(
        service, mode="manual", max_cycles=1, max_claims=10, max_proposals=10,
        max_probe_files=5, apply=False,
        artifact_path=workspace / "artifacts" / "steward_report.json",
        enable_semantic_probe=False, enable_tool_probe=False,
        enable_contradiction_probe=False,  # OFF
    )
    cycle = report["cycles"][0]
    by_id = {int(d["claim_id"]): d for d in cycle["decisions"]}
    for d in by_id.values():
        codes = {r["code"] for r in d["reasons"]}
        assert "contradiction_probe.semantic_pair" not in codes
    assert calls["n"] == 0  # judge was never invoked


def test_report_includes_enable_contradiction_probe_flag() -> None:
    db = _case_db("steward-contra-flag")
    workspace = _case_workspace("steward-contra-flag-ws")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    report = run_steward(
        service, mode="manual", max_cycles=1, max_claims=1, max_proposals=1,
        max_probe_files=1, apply=False,
        artifact_path=workspace / "artifacts" / "steward_report.json",
        enable_semantic_probe=False, enable_tool_probe=False,
        enable_contradiction_probe=False,
    )
    assert report["enable_contradiction_probe"] is False
    assert report["run_metadata"]["probe_settings"]["enable_contradiction_probe"] is False


def _seed_topical_trio(service, db):
    """3 mutually-topical claims with distinct subject+predicate (so the
    deterministic resolver doesn't own them) — each is a contradiction peer of
    the others for the probe."""
    ids = []
    specs = [
        ("api rate limit", "value", "The public API rate limit policy is documented here."),
        ("api throttling", "state", "The public API rate limit behaviour is described here."),
        ("api quota", "tier", "The public API rate limit tiers are listed here."),
    ]
    for i, (subj, pred, text) in enumerate(specs):
        c = service.ingest(
            text=text, citations=[CitationInput(source="d", locator=f"l{i}")],
            subject=subj, predicate=pred, object_value=f"v{i}", confidence=0.4 + i * 0.1,
        )
        _force_status(db, c.id, "confirmed", f"2026-0{i + 1}-01T00:00:00+00:00")
        ids.append(c.id)
    return ids


def test_budget_cap_enforced_across_cycle_and_not_a_circuit_failure(monkeypatch) -> None:
    """M1: the steward now opens one llm_budget cycle_scope, so
    MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE=1 caps the contradiction judge across
    the WHOLE cycle (exactly 1 judge call; without the scope all distinct pairs
    judge => 3). M2: budget exhaustion is flagged budget_exhausted (not
    timed_out) and must NOT trip the circuit breaker even at threshold=1."""
    db = _case_db("steward-budget")
    workspace = _case_workspace("steward-budget-ws")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()

    calls = {"n": 0}

    def _always_contradict(prompt, body):
        calls["n"] += 1
        return json.dumps({"contradicts": True, "severity": "high", "reason": "conflict"})

    monkeypatch.setitem(llm_provider._PROVIDERS, "google", _always_contradict)
    monkeypatch.setenv("MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE", "1")
    reset_config()
    _seed_topical_trio(service, db)

    report = run_steward(
        service, mode="manual", max_cycles=1, max_claims=10, max_proposals=10,
        max_probe_files=5, apply=False, probe_failure_threshold=1,
        artifact_path=workspace / "artifacts" / "steward_report.json",
        enable_semantic_probe=False, enable_tool_probe=False,
        enable_contradiction_probe=True,
    )

    # M1: the per-cycle cap held across claims — exactly one judge call total.
    assert calls["n"] == 1, f"expected 1 judge call under cap=1, got {calls['n']}"

    cycle = report["cycles"][0]
    totals = cycle["budget"]["accounting"]["probes"]
    # M2: budget exhaustion was not counted as a timeout/error and did not open
    # the circuit (threshold=1 would have tripped on the first counted failure).
    assert totals["timed_out"] == 0
    assert totals["errored"] == 0
    assert totals["skipped_circuit_open"] == 0

    # M2: at least one claim's contradiction probe recorded budget_exhausted.
    saw_budget_exhausted = False
    for d in cycle["decisions"]:
        for p in d["probes"]:
            if p["probe_type"] == "contradiction_probe" and p["metrics"].get("budget_exhausted"):
                saw_budget_exhausted = True
    assert saw_budget_exhausted, "expected a contradiction probe to flag budget_exhausted"
