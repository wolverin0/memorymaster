"""Recall Pattern Analyzer — ranking explainability (observability only).

These tests encode the REQUIREMENT, not the implementation: an operator/agent
must be able to see WHY a claim ranked where it did. Concretely:

- every ranked claim carries the full score breakdown (raw components, weighted
  contributions, tier/pinned bonuses, relevance/boost subtotals, floor-gate
  status, final score);
- the reported weights match what ``config`` actually applied;
- floor-gate suppression is visible in the breakdown;
- per-component claim rankings reflect the per-signal order;
- the CLI ``recall-analysis`` command and the MCP ``recall_analysis`` tool both
  return that breakdown.

The analyzer is OBSERVABILITY: it must never change ranking order. One test
pins that the analyzer's order equals the ranker's order.
"""
from __future__ import annotations

import argparse

import pytest

from memorymaster.config import get_config, reset_config
from memorymaster.models import Claim, CitationInput
from memorymaster.retrieval import component_rankings, rank_claim_rows
from memorymaster.service import MemoryService

_OLD = "2000-01-01T00:00:00+00:00"
_FUTURE = "2999-01-01T00:00:00+00:00"
_QUERY = "postgresql database version"

# Required keys the breakdown must expose so explainability is complete.
_REQUIRED_BREAKDOWN_KEYS = {
    "components",
    "contributions",
    "weights_applied",
    "tier_bonus",
    "pinned_bonus",
    "relevance_subtotal",
    "boosts_subtotal",
    "final_score",
    "floor_gated",
}
_REQUIRED_COMPONENTS = {"lexical", "confidence", "freshness", "vector"}


def _claim(cid: int, text: str, *, confidence: float, pinned: bool, date: str) -> Claim:
    return Claim(
        id=cid, text=text, idempotency_key=None, normalized_text=None,
        claim_type=None, subject=None, predicate=None, object_value=None,
        scope="project", volatility="medium", status="confirmed",
        confidence=confidence, pinned=pinned, supersedes_claim_id=None,
        replaced_by_claim_id=None, created_at=date, updated_at=date,
        last_validated_at=None, archived_at=None,
    )


def _claims() -> list[Claim]:
    true_match = _claim(
        1, "the production server uses postgresql database version sixteen",
        confidence=0.0, pinned=False, date=_OLD,
    )
    decoy = _claim(
        2, "postgresql tutorial introduction beginners guide notes",
        confidence=1.0, pinned=True, date=_FUTURE,
    )
    return [true_match, decoy]


@pytest.fixture(autouse=True)
def _reset_cfg():
    reset_config()
    yield
    reset_config()


# --------------------------------------------------------------------------
# Breakdown completeness (retrieval layer)
# --------------------------------------------------------------------------

def test_every_ranked_claim_carries_full_breakdown():
    """Requirement: an operator can see every score component for every claim."""
    rows = rank_claim_rows(_QUERY, _claims(), mode="hybrid", limit=5)
    assert rows
    for row in rows:
        bd = row.breakdown
        assert bd is not None
        missing = _REQUIRED_BREAKDOWN_KEYS - bd.keys()
        assert not missing, f"breakdown missing keys: {missing}"
        assert _REQUIRED_COMPONENTS <= bd["components"].keys()
        assert _REQUIRED_COMPONENTS <= bd["weights_applied"].keys()


def test_subtotals_sum_to_final_score():
    """relevance + (gated ? 0 : boosts) must equal the final score the ranker
    used — the breakdown explains the *actual* number, not a recomputation."""
    rows = rank_claim_rows(_QUERY, _claims(), mode="hybrid", limit=5)
    for row in rows:
        bd = row.breakdown
        expected = bd["relevance_subtotal"] + bd["boosts_subtotal"]
        assert bd["final_score"] == pytest.approx(expected)
        assert bd["final_score"] == pytest.approx(row.score)


def test_weights_in_breakdown_match_config():
    """The reported weights must be the ones config actually shipped, so an
    operator debugging a rank is not shown stale/guessed weights."""
    rows = rank_claim_rows(_QUERY, _claims(), mode="hybrid", limit=5)
    cfg = get_config()
    # No vector hook in this scenario -> no-vector blend; vector weight is 0.
    w_l, w_c, w_f = cfg.retrieval_weights_no_vector
    for row in rows:
        wa = row.breakdown["weights_applied"]
        assert wa["lexical"] == pytest.approx(w_l)
        assert wa["confidence"] == pytest.approx(w_c)
        assert wa["freshness"] == pytest.approx(w_f)
        assert wa["vector"] == pytest.approx(0.0)


def test_floor_gate_suppression_is_visible(monkeypatch):
    """When the floor gate suppresses a weak match's boosts, the breakdown must
    show floor_gated=True AND a zeroed boost subtotal — that's the whole point
    of the analyzer (explaining a surprising demotion)."""
    monkeypatch.setenv("MEMORYMASTER_BOOST_FLOOR_RATIO", "0.6")
    reset_config()
    rows = {r.claim.id: r for r in rank_claim_rows(_QUERY, _claims(), mode="hybrid", limit=5)}
    # Decoy (id=2) matches only 1 token -> below floor -> boosts gated off.
    decoy = rows[2].breakdown
    assert decoy["floor_gated"] is True
    assert decoy["boosts_subtotal"] == pytest.approx(0.0)
    assert decoy["final_score"] == pytest.approx(decoy["relevance_subtotal"])
    # True match (id=1) is the top lexical hit -> boosts apply.
    assert rows[1].breakdown["floor_gated"] is False


def test_tier_and_pinned_bonuses_surface_in_contributions():
    """pinned/tier bonuses are part of WHY a claim ranked — they must appear as
    named contributions, not be hidden inside an opaque total."""
    rows = {r.claim.id: r for r in rank_claim_rows(_QUERY, _claims(), mode="hybrid", limit=5)}
    decoy = rows[2].breakdown  # pinned=True
    assert decoy["pinned_bonus"] == pytest.approx(get_config().pinned_bonus)
    assert decoy["contributions"]["pinned_bonus"] == pytest.approx(get_config().pinned_bonus)


# --------------------------------------------------------------------------
# Component rankings
# --------------------------------------------------------------------------

def test_component_rankings_reflect_per_signal_order():
    """The per-component ranking for 'confidence' must order claims by their
    confidence score (decoy conf=1.0 before true-match conf=0.0)."""
    rows = rank_claim_rows(_QUERY, _claims(), mode="hybrid", limit=5)
    rankings = component_rankings(rows)
    assert set(rankings) == _REQUIRED_COMPONENTS
    # Decoy (id=2, conf 1.0) ranks above true match (id=1, conf 0.0) on confidence.
    assert rankings["confidence"].index(2) < rankings["confidence"].index(1)
    # True match (id=1) is the stronger lexical hit -> ranks first on lexical.
    assert rankings["lexical"][0] == 1


# --------------------------------------------------------------------------
# Service layer
# --------------------------------------------------------------------------

@pytest.fixture
def svc(tmp_path):
    reset_config()
    s = MemoryService(tmp_path / "ra.db", workspace_root=tmp_path)
    s.init_db()
    s.ingest(
        text="the production server uses postgresql database version sixteen",
        citations=[CitationInput(source="fix", locator="c1")],
        source_agent="ra-fixture",
    )
    s.ingest(
        text="postgresql tutorial introduction beginners guide notes",
        citations=[CitationInput(source="fix", locator="c2")],
        source_agent="ra-fixture",
    )
    yield s
    reset_config()


def test_service_recall_analysis_returns_breakdown_and_weights(svc):
    analysis = svc.recall_analysis(
        _QUERY, retrieval_mode="hybrid", include_candidates=True, limit=5
    )
    assert analysis["query"] == _QUERY
    assert analysis["mode"] == "hybrid"
    assert analysis["results"], "expected ranked results"
    # Weights snapshot matches config.
    cfg = get_config()
    rw = analysis["weights"]["retrieval_weights"]
    assert rw["lexical"] == pytest.approx(cfg.retrieval_weights[0])
    assert analysis["weights"]["boost_floor_ratio"] == pytest.approx(cfg.boost_floor_ratio)
    # Every result entry carries a complete breakdown.
    for entry in analysis["results"]:
        assert _REQUIRED_BREAKDOWN_KEYS <= entry["breakdown"].keys()
    # Component rankings present and id-typed.
    assert set(analysis["component_rankings"]) == _REQUIRED_COMPONENTS


def test_service_recall_analysis_preserves_ranking_order(svc):
    """Observability must not reorder results: the analyzer's order equals the
    ranker's order from query_rows."""
    rows = svc.query_rows(
        query_text=_QUERY, retrieval_mode="hybrid", include_candidates=True, limit=5
    )
    analysis = svc.recall_analysis(
        _QUERY, retrieval_mode="hybrid", include_candidates=True, limit=5
    )
    ranker_ids = [r["claim"].id for r in rows]
    analyzer_ids = [e["claim_id"] for e in analysis["results"]]
    assert analyzer_ids == ranker_ids


# --------------------------------------------------------------------------
# CLI handler
# --------------------------------------------------------------------------

def test_cli_recall_analysis_json_envelope(svc, capsys):
    from memorymaster.cli_handlers_basic import _handle_recall_analysis

    args = argparse.Namespace(
        query=_QUERY, mode="hybrid", limit=5, profile=None,
        include_candidates=True, allow_sensitive=False, scope_allowlist="",
        json_output=True,
    )
    rc = _handle_recall_analysis(args, svc, None, "ra.db")
    assert rc == 0
    out = capsys.readouterr().out
    import json as _json
    payload = _json.loads(out)
    assert payload["ok"] is True
    assert payload["data"]["results"]
    assert _REQUIRED_BREAKDOWN_KEYS <= payload["data"]["results"][0]["breakdown"].keys()


def test_cli_recall_analysis_human_output(svc, capsys):
    from memorymaster.cli_handlers_basic import _handle_recall_analysis

    args = argparse.Namespace(
        query=_QUERY, mode="hybrid", limit=5, profile=None,
        include_candidates=True, allow_sensitive=False, scope_allowlist="",
        json_output=False,
    )
    rc = _handle_recall_analysis(args, svc, None, "ra.db")
    assert rc == 0
    out = capsys.readouterr().out
    assert "components:" in out
    assert "component rankings" in out


# --------------------------------------------------------------------------
# MCP tool
# --------------------------------------------------------------------------

def test_mcp_recall_analysis_tool_returns_breakdown(tmp_path):
    try:
        from memorymaster.mcp_server import init_db, ingest_claim, recall_analysis
    except ImportError:
        pytest.skip("MCP not installed")

    db = str(tmp_path / "mcp.db")
    ws = str(tmp_path)
    init_db(db=db, workspace=ws)
    ingest_claim(
        text="the production server uses postgresql database version sixteen",
        db=db, workspace=ws, sources_json='["fix"]',
    )
    result = recall_analysis(
        query=_QUERY, db=db, workspace=ws,
        retrieval_mode="hybrid", include_candidates=True, limit=5,
    )
    assert result["ok"] is True
    assert result["results"]
    assert _REQUIRED_BREAKDOWN_KEYS <= result["results"][0]["breakdown"].keys()
    # Weights snapshot is present so an agent can see the active blend.
    assert "retrieval_weights" in result["weights"]


# --------------------------------------------------------------------------
# Dashboard endpoint (thin wrapper)
# --------------------------------------------------------------------------

def test_dashboard_recall_analysis_endpoint_returns_breakdown(svc):
    """The /api/recall-analysis GET wrapper must surface the same breakdown,
    delegating to service.recall_analysis without touching ranking math."""
    from types import SimpleNamespace

    from memorymaster.dashboard import DashboardRequestHandler

    captured: dict = {}

    fake = SimpleNamespace(
        _server=SimpleNamespace(service=svc),
        _write_json=lambda payload, **_: captured.update(payload),
    )
    # Bind the real handler method to our minimal fake (no socket/server needed).
    DashboardRequestHandler._handle_recall_analysis(
        fake, f"query={_QUERY}&mode=hybrid&limit=5"
    )
    assert captured["ok"] is True
    assert captured["results"]
    assert _REQUIRED_BREAKDOWN_KEYS <= captured["results"][0]["breakdown"].keys()
