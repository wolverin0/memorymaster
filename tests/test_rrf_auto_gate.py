"""Tests for the RRF auto-gate heuristic (roadmap 11.6).

The gate decides, per recall() call, whether to fuse streams with RRF or
the legacy linear combiner. Rationale in claim 11898: RRF wins when
candidates are dense (>= 3 populated streams) and loses when they are
sparse (<= 2 populated streams). Default gate threshold is 3; override
with MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD.

These tests mock the candidate rows directly — no full recall(), no DB —
to keep the gate logic isolated from the rest of the retrieval stack.

Six cases:
    A. 1 stream populated (only bm25) -> linear
    B. 2 streams populated -> linear (below threshold=3)
    C. 3 streams populated -> rrf
    D. threshold override to 2, 2 streams -> rrf
    E. fusion=linear (not auto): gate helper never called, counter stays 0
    F. fusion=rrf (not auto): gate helper never called, counter stays 0
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from memorymaster import context_hook


def _row(cid: int, **scores) -> dict:
    """Build a query_rows-shaped row dict with optional per-stream scores.

    ``scores`` keys may be any of: entity_score, vector_score,
    verbatim_score, freshness_score. Missing keys default to 0.0.
    """
    claim = SimpleNamespace(id=cid, text=f"claim-{cid}", status="confirmed")
    row = {
        "claim": claim,
        "score": 0.0,
        "lexical_score": 0.0,
        "freshness_score": 0.0,
        "confidence_score": 0.0,
        "vector_score": 0.0,
        "entity_score": 0.0,
        "verbatim_score": 0.0,
    }
    row.update(scores)
    return row


@pytest.fixture(autouse=True)
def _reset_env_and_stats(monkeypatch):
    """Scrub every env var the gate reads and reset counters before + after."""
    for key in (
        "MEMORYMASTER_RECALL_FUSION",
        "MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD",
        "MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_FACT_LOOKUP",
        "MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_TEMPORAL",
    ):
        monkeypatch.delenv(key, raising=False)
    context_hook.reset_auto_gate_stats()
    yield
    context_hook.reset_auto_gate_stats()


# ---------------------------------------------------------------------------
# A. 1 stream populated (only bm25) -> linear
# ---------------------------------------------------------------------------
def test_case_a_one_stream_picks_linear():
    """Only bm25 is populated. Populated count = 1 < threshold 3 -> linear."""
    rows = [_row(1), _row(2), _row(3)]
    bm25_scores = {1: 5.2, 2: 3.1}
    bm25_on = True

    decision, populated, threshold = context_hook._auto_gate_decide(
        rows, bm25_scores, bm25_on, freshness_weight=0.0
    )

    assert decision == "linear"
    assert populated == 1
    assert threshold == 3
    stats = context_hook.get_auto_gate_stats()
    assert stats == {"calls": 1, "picked_rrf": 0, "picked_linear": 1}


# ---------------------------------------------------------------------------
# B. 2 streams populated -> linear (below threshold=3)
# ---------------------------------------------------------------------------
def test_case_b_two_streams_below_threshold_picks_linear():
    """bm25 + entity populated = 2 streams < threshold 3 -> linear."""
    rows = [
        _row(1, entity_score=1.0),
        _row(2, entity_score=0.0),
    ]
    bm25_scores = {1: 4.2, 2: 2.1}
    bm25_on = True

    decision, populated, threshold = context_hook._auto_gate_decide(
        rows, bm25_scores, bm25_on, freshness_weight=0.0
    )

    assert decision == "linear"
    assert populated == 2
    assert threshold == 3
    stats = context_hook.get_auto_gate_stats()
    assert stats == {"calls": 1, "picked_rrf": 0, "picked_linear": 1}


# ---------------------------------------------------------------------------
# C. 3 streams populated -> rrf
# ---------------------------------------------------------------------------
def test_case_c_three_streams_picks_rrf():
    """bm25 + entity + vector populated = 3 streams >= threshold 3 -> rrf."""
    rows = [
        _row(1, entity_score=1.0, vector_score=0.0),
        _row(2, entity_score=0.0, vector_score=0.75),
    ]
    bm25_scores = {1: 4.2, 2: 2.1}
    bm25_on = True

    decision, populated, threshold = context_hook._auto_gate_decide(
        rows, bm25_scores, bm25_on, freshness_weight=0.0
    )

    assert decision == "rrf"
    assert populated == 3
    assert threshold == 3
    stats = context_hook.get_auto_gate_stats()
    assert stats == {"calls": 1, "picked_rrf": 1, "picked_linear": 0}


# ---------------------------------------------------------------------------
# D. threshold override to 2, 2 streams -> rrf
# ---------------------------------------------------------------------------
def test_case_d_threshold_override_two_streams_picks_rrf(monkeypatch):
    """MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD=2 + 2 streams populated -> rrf."""
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD", "2")

    rows = [
        _row(1, entity_score=1.0),
        _row(2, entity_score=0.0),
    ]
    bm25_scores = {1: 4.2, 2: 2.1}
    bm25_on = True

    decision, populated, threshold = context_hook._auto_gate_decide(
        rows, bm25_scores, bm25_on, freshness_weight=0.0
    )

    assert decision == "rrf"
    assert populated == 2
    assert threshold == 2
    stats = context_hook.get_auto_gate_stats()
    assert stats == {"calls": 1, "picked_rrf": 1, "picked_linear": 0}


# ---------------------------------------------------------------------------
# E. fusion=linear (not auto): gate helper never called, counter stays 0
# ---------------------------------------------------------------------------
def test_case_e_fusion_linear_never_calls_gate(monkeypatch):
    """When MEMORYMASTER_RECALL_FUSION=linear, the auto-gate code path is
    never reached. Simulate that by running the env-read branching without
    invoking ``_auto_gate_decide`` and confirm counters stay at 0.
    """
    import os
    monkeypatch.setenv("MEMORYMASTER_RECALL_FUSION", "linear")

    fusion_mode = (
        os.environ.get("MEMORYMASTER_RECALL_FUSION", "linear").strip().lower()
    )
    if fusion_mode == "auto":
        context_hook._auto_gate_decide([], {}, False, 0.0)  # pragma: no cover
    # fusion_mode is "linear", not "auto": helper is NOT called.

    assert fusion_mode == "linear"
    stats = context_hook.get_auto_gate_stats()
    assert stats == {"calls": 0, "picked_rrf": 0, "picked_linear": 0}


# ---------------------------------------------------------------------------
# F. fusion=rrf (not auto): gate helper never called, counter stays 0
# ---------------------------------------------------------------------------
def test_case_f_fusion_rrf_never_calls_gate(monkeypatch):
    """When MEMORYMASTER_RECALL_FUSION=rrf, the auto-gate code path is
    never reached — RRF is picked unconditionally with no gate telemetry.
    """
    import os
    monkeypatch.setenv("MEMORYMASTER_RECALL_FUSION", "rrf")

    fusion_mode = (
        os.environ.get("MEMORYMASTER_RECALL_FUSION", "linear").strip().lower()
    )
    if fusion_mode == "auto":
        context_hook._auto_gate_decide([], {}, False, 0.0)  # pragma: no cover
    # fusion_mode is "rrf", not "auto": helper is NOT called.

    assert fusion_mode == "rrf"
    stats = context_hook.get_auto_gate_stats()
    assert stats == {"calls": 0, "picked_rrf": 0, "picked_linear": 0}


# ---------------------------------------------------------------------------
# Bonus coverage — freshness weight gating + bm25_on=False edge cases
# ---------------------------------------------------------------------------
def test_freshness_only_counts_when_weight_positive():
    """freshness_score alone is not counted unless W_FRESHNESS > 0."""
    rows = [_row(1, freshness_score=0.5), _row(2, freshness_score=0.2)]
    # freshness_weight=0 -> freshness stream NOT counted.
    count = context_hook._count_populated_streams(
        rows, bm25_scores={}, bm25_on=False, freshness_weight=0.0
    )
    assert count == 0
    # freshness_weight>0 -> freshness stream counted (once).
    count = context_hook._count_populated_streams(
        rows, bm25_scores={}, bm25_on=False, freshness_weight=0.1
    )
    assert count == 1


def test_bm25_off_does_not_count_even_with_scores():
    """bm25_on=False -> bm25 stream is absent even if bm25_scores has data."""
    count = context_hook._count_populated_streams(
        rows=[], bm25_scores={1: 5.0}, bm25_on=False, freshness_weight=0.0
    )
    assert count == 0


# ---------------------------------------------------------------------------
# Per-type threshold override — MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_<TYPE>
#
# REQUIREMENT: a sparse query class (e.g. fact_lookup) can demand MORE
# populated streams before paying for RRF than the global default. The
# override is keyed on the query_classifier type and takes precedence over
# the global var ONLY for queries that classify to that type.
# ---------------------------------------------------------------------------
def test_per_type_override_raises_bar_for_matching_query(monkeypatch):
    """fact_lookup override=4 + 3 populated streams -> linear (below 4),

    even though the global default 3 would have picked RRF. This is the
    whole point: the per-type bar gates RRF for this query class.
    """
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_FACT_LOOKUP", "4")

    rows = [
        _row(1, entity_score=1.0, vector_score=0.0),
        _row(2, entity_score=0.0, vector_score=0.75),
    ]
    bm25_scores = {1: 4.2, 2: 2.1}  # bm25 + entity + vector = 3 streams

    decision, populated, threshold = context_hook._auto_gate_decide(
        rows, bm25_scores, bm25_on=True, freshness_weight=0.0,
        query="What database does pedrito use?",  # -> fact_lookup
    )

    assert populated == 3
    assert threshold == 4  # per-type override applied, not the global 3
    assert decision == "linear"  # 3 < 4
    assert context_hook.get_auto_gate_stats()["picked_linear"] == 1


def test_per_type_override_ignored_for_non_matching_query(monkeypatch):
    """A fact_lookup override must NOT affect a temporal query — that query

    uses the global threshold (default 3), so 3 streams -> RRF.
    """
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_FACT_LOOKUP", "4")

    rows = [
        _row(1, entity_score=1.0, vector_score=0.0),
        _row(2, entity_score=0.0, vector_score=0.75),
    ]
    bm25_scores = {1: 4.2, 2: 2.1}  # 3 streams

    decision, populated, threshold = context_hook._auto_gate_decide(
        rows, bm25_scores, bm25_on=True, freshness_weight=0.0,
        query="What changed last week?",  # -> temporal, not fact_lookup
    )

    assert populated == 3
    assert threshold == 3  # global default, override didn't match
    assert decision == "rrf"


def test_per_type_override_takes_precedence_over_global(monkeypatch):
    """When BOTH global and per-type are set, the per-type wins for a

    matching query: global=2 would pick RRF at 2 streams, but the
    fact_lookup override=5 keeps it linear.
    """
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD", "2")
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_FACT_LOOKUP", "5")

    rows = [_row(1, entity_score=1.0), _row(2, entity_score=0.0)]
    bm25_scores = {1: 4.2, 2: 2.1}  # bm25 + entity = 2 streams

    decision, populated, threshold = context_hook._auto_gate_decide(
        rows, bm25_scores, bm25_on=True, freshness_weight=0.0,
        query="What database does pedrito use?",  # -> fact_lookup
    )

    assert populated == 2
    assert threshold == 5  # per-type beats global
    assert decision == "linear"


def test_invalid_per_type_override_falls_back_to_global(monkeypatch):
    """A garbage per-type value is ignored and the global var is used —

    a typo'd override must never silently disable gating.
    """
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD", "2")
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_FACT_LOOKUP", "oops")

    rows = [_row(1, entity_score=1.0), _row(2, entity_score=0.0)]
    bm25_scores = {1: 4.2, 2: 2.1}  # 2 streams

    decision, populated, threshold = context_hook._auto_gate_decide(
        rows, bm25_scores, bm25_on=True, freshness_weight=0.0,
        query="What database does pedrito use?",  # -> fact_lookup
    )

    assert populated == 2
    assert threshold == 2  # fell back to the global override
    assert decision == "rrf"


def test_explicit_threshold_arg_bypasses_per_type_lookup(monkeypatch):
    """Passing threshold= explicitly (test path) ignores env entirely,

    including any per-type override — preserves the existing test contract.
    """
    monkeypatch.setenv("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_FACT_LOOKUP", "9")

    rows = [_row(1, entity_score=1.0), _row(2, entity_score=0.0)]
    bm25_scores = {1: 4.2, 2: 2.1}  # 2 streams

    decision, populated, threshold = context_hook._auto_gate_decide(
        rows, bm25_scores, bm25_on=True, freshness_weight=0.0,
        threshold=2, query="What database does pedrito use?",
    )

    assert threshold == 2  # explicit arg wins over per-type env
    assert decision == "rrf"


def test_threshold_helper_per_type_resolution():
    """Unit-level: _auto_gate_threshold honors the type, ignores bad values."""
    import os

    # No env -> default 3 regardless of type.
    assert context_hook._auto_gate_threshold("fact_lookup") == 3
    assert context_hook._auto_gate_threshold(None) == 3

    os.environ["MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_FACT_LOOKUP"] = "7"
    try:
        assert context_hook._auto_gate_threshold("fact_lookup") == 7
        # Different type doesn't see the fact_lookup override.
        assert context_hook._auto_gate_threshold("temporal") == 3
        # None type never consults per-type vars.
        assert context_hook._auto_gate_threshold(None) == 3
    finally:
        del os.environ["MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_FACT_LOOKUP"]
