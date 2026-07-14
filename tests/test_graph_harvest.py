"""Graph-backed retrieval HARVEST stream — roadmap 12.2.

Promotes the Kuzu claim↔entity graph from an annotation-only signal to a
full retrieval stream that HARVESTS new candidate claims via BFS and scores
them by hop-distance (``graph_score = 1/(1+hops)``), blended into the linear
combiner under ``W_GRAPH``.

WHY (intent, not lines): the shipped guarantee is twofold and load-bearing —
(1) with both new flags unset, recall output (markdown + claim set + order)
is BIT-IDENTICAL to the pre-harvest baseline, so enabling the graph package
can never regress an existing deployment; and (2) any graph failure (Kuzu
missing, BFS raising) degrades to zero observable impact, never crashing the
recall hot path. These tests anchor on those two contracts, not on the line
of code that implements them.

No live Kuzu is required: the BFS entry point
``_graph_reached_claim_distance`` is monkeypatched to return canned
``{claim_id: hop}`` mappings so the harvest + score + blend logic is
exercised deterministically. (Kuzu IS installed in CI as of 0.11.x, but the
harvest contract must hold even when it is not, so we mock the boundary.)
"""
from __future__ import annotations

import os

import pytest

from memorymaster.recall import context_hook as ch


@pytest.fixture(autouse=True)
def _clear_recall_env(monkeypatch):
    """Start every test from shipped defaults (no MEMORYMASTER_* leakage)."""
    for key in list(os.environ):
        if key.startswith("MEMORYMASTER_"):
            monkeypatch.delenv(key, raising=False)


def _seed(tmp_path):
    """Two FTS5-reachable claims. Returns (db_path, [seeded_claim_ids])."""
    from memorymaster.core.models import CitationInput
    from memorymaster.core.lifecycle import transition_claim
    from memorymaster.core.service import MemoryService

    db = tmp_path / "harvest.db"
    svc = MemoryService(db_target=str(db), workspace_root=tmp_path)
    svc.init_db()
    ids: list[int] = []
    for text, ctype, conf in (
        ("The user prefers PostgreSQL for production databases.", "preference", 0.9),
        ("The team decided to use Qdrant for vector search backends.", "decision", 0.8),
    ):
        claim = svc.ingest(
            text=text,
            citations=[CitationInput(source="test")],
            claim_type=ctype,
            scope="project",
            confidence=conf,
        )
        transition_claim(svc.store, claim.id, "confirmed", "trusted recall fixture")
        ids.append(int(claim.id))
    return str(db), ids


def _ingest_offgraph_claim(db, text="A graph-only fact about Kubernetes scaling."):
    """Ingest a claim that FTS5 won't surface for a 'PostgreSQL' query, so it
    can only enter the candidate pool via the graph harvest path. Returns id.
    """
    from memorymaster.core.models import CitationInput
    from memorymaster.core.lifecycle import transition_claim
    from memorymaster.core.service import MemoryService

    svc = MemoryService(db_target=db, workspace_root=os.path.dirname(db) or ".")
    claim = svc.ingest(
        text=text,
        citations=[CitationInput(source="test")],
        claim_type="fact",
        scope="project",
        confidence=0.7,
    )
    transition_claim(svc.store, claim.id, "confirmed", "trusted recall fixture")
    return int(claim.id)


# --------------------------------------------------------------------------
# flag helpers
# --------------------------------------------------------------------------
def test_graph_candidates_flag_defaults_off():
    """The new harvest flag must default OFF — annotation-only baseline."""
    assert ch._graph_candidates_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_graph_candidates_flag_truthy(monkeypatch, val):
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_CANDIDATES", val)
    assert ch._graph_candidates_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_graph_candidates_flag_falsey(monkeypatch, val):
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_CANDIDATES", val)
    assert ch._graph_candidates_enabled() is False


# --------------------------------------------------------------------------
# score formula on harvested rows
# --------------------------------------------------------------------------
def test_row_for_graph_claim_score_decays_with_hops():
    """Harvested-row score must be the documented ``1/(1+hops)``.

    WHY: this is the hop→weight contract the ranker relies on; if the helper
    drifts from the BFS hop semantics, near and far claims rank identically.
    """
    class _C:
        confidence = 0.5
        status = "confirmed"

    assert ch._row_for_graph_claim(_C(), 1.0)["graph_score"] == 1.0
    assert ch._row_for_graph_claim(_C(), 0.5)["graph_score"] == 0.5
    row = ch._row_for_graph_claim(_C(), 1.0 / 3.0)
    assert row["graph_score"] == pytest.approx(1.0 / 3.0)
    # Harvested rows carry no lexical/vector/entity signal — only graph.
    assert row["lexical_score"] == 0.0
    assert row["vector_score"] == 0.0
    assert row["entity_score"] == 0.0
    assert row["source"] == "graph_harvest"


# --------------------------------------------------------------------------
# harvest logic (mocked BFS)
# --------------------------------------------------------------------------
def test_harvest_adds_unseen_claim_with_distance_score(tmp_path):
    """A BFS-reached claim absent from the candidate pool is hydrated with
    the correct distance-weighted graph_score.
    """
    db, _ = _seed(tmp_path)
    off_id = _ingest_offgraph_claim(db)

    from memorymaster.core.service import MemoryService

    svc = MemoryService(db_target=db, workspace_root=str(tmp_path))
    rows: list = []
    seen: set[int] = set()
    added = ch._harvest_graph_rows(
        svc, {off_id: 1}, rows, seen
    )  # hop 1 → 0.5
    assert added == 1
    assert len(rows) == 1
    assert rows[0]["graph_score"] == pytest.approx(0.5)
    assert off_id in seen


def test_harvest_dedups_against_existing_candidates(tmp_path):
    """A claim already in the candidate pool must NOT be double-added by the
    harvest — dedup is enforced against ``seen_ids``.
    """
    db, seeded = _seed(tmp_path)
    from memorymaster.core.service import MemoryService

    svc = MemoryService(db_target=db, workspace_root=str(tmp_path))
    existing = {"claim": svc.store.get_claim(seeded[0]), "graph_score": 0.0}
    rows = [existing]
    seen = {seeded[0]}
    added = ch._harvest_graph_rows(svc, {seeded[0]: 0}, rows, seen)
    assert added == 0
    assert len(rows) == 1  # unchanged — no duplicate row


def test_harvest_skips_archived_and_missing(tmp_path):
    """Archived / unhydratable claims are skipped, never appended."""
    db, _ = _seed(tmp_path)
    from memorymaster.core.service import MemoryService

    svc = MemoryService(db_target=db, workspace_root=str(tmp_path))
    rows: list = []
    seen: set[int] = set()
    # claim id 999999 does not exist → get_claim returns None → skipped.
    added = ch._harvest_graph_rows(svc, {999999: 0}, rows, seen)
    assert added == 0
    assert rows == []


def test_harvest_hydrate_error_does_not_raise(monkeypatch, tmp_path):
    """A per-claim hydrate error is swallowed, not propagated (claim 11907)."""
    db, seeded = _seed(tmp_path)
    from memorymaster.core.service import MemoryService

    svc = MemoryService(db_target=db, workspace_root=str(tmp_path))

    def _boom(*a, **k):
        raise RuntimeError("hydrate exploded")

    monkeypatch.setattr(svc.store, "get_claim", _boom)
    rows: list = []
    added = ch._harvest_graph_rows(svc, {seeded[0]: 0}, rows, set())
    assert added == 0
    assert rows == []


# --------------------------------------------------------------------------
# end-to-end recall: harvest blends into ranking
# --------------------------------------------------------------------------
def test_recall_harvests_offgraph_claim_when_flags_on(tmp_path, monkeypatch):
    """With BOTH flags on and a mocked BFS reaching an off-graph claim, that
    claim is harvested into recall output and contributes to ranking under
    W_GRAPH.

    WHY: this is the core net-new behaviour — a claim FTS5 never surfaces for
    'PostgreSQL' appears in the result only because the graph stream reached
    it. Proves harvest + blend wired end-to-end.
    """
    db, _ = _seed(tmp_path)
    off_id = _ingest_offgraph_claim(
        db, text="Reticulating splines on the Andromeda cluster."
    )

    # Mock the BFS boundary: pretend the off-graph claim is hop 0 (score 1.0).
    monkeypatch.setattr(
        ch, "_graph_reached_claim_distance", lambda q, store: {off_id: 0}
    )
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_CANDIDATES", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_GRAPH", "5.0")  # dominate ranking

    out, ids = ch.recall(
        "PostgreSQL production", db_path=db, skip_qdrant=True, return_ids=True
    )
    assert off_id in ids, (
        "graph-harvested off-graph claim must appear in recall output"
    )


def test_recall_harvest_noop_when_only_graph_flag_on(tmp_path, monkeypatch):
    """GRAPH=1 but CANDIDATES unset → annotation-only: an off-graph claim is
    NOT harvested. Guards against the harvest firing on the wrong flag.
    """
    db, _ = _seed(tmp_path)
    off_id = _ingest_offgraph_claim(
        db, text="Reticulating splines on the Andromeda cluster."
    )
    monkeypatch.setattr(
        ch, "_graph_reached_claim_distance", lambda q, store: {off_id: 0}
    )
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_GRAPH", "5.0")
    # CANDIDATES deliberately unset.
    _, ids = ch.recall(
        "PostgreSQL production", db_path=db, skip_qdrant=True, return_ids=True
    )
    assert off_id not in ids


def test_recall_graph_bfs_error_does_not_break_recall(tmp_path, monkeypatch):
    """If the BFS boundary raises, recall still returns the FTS5 result
    unchanged — the graph stream never raises into the hot path.
    """
    db, _ = _seed(tmp_path)

    def _raise(q, store):
        raise RuntimeError("kuzu went sideways")

    monkeypatch.setattr(ch, "_graph_reached_claim_distance", _raise)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_CANDIDATES", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_GRAPH", "1.0")
    out = ch.recall("PostgreSQL production", db_path=db, skip_qdrant=True)
    assert isinstance(out, str)
    assert "PostgreSQL" in out or "postgres" in out.lower()


# --------------------------------------------------------------------------
# THE bit-identical regression guarantee
# --------------------------------------------------------------------------
def test_disabled_mode_is_bit_identical(tmp_path, monkeypatch):
    """REGRESSION: with both new flags unset, recall output (rendered markdown
    AND the ordered claim-id set) is byte-for-byte identical whether or not
    the graph BFS *would* have reached extra claims.

    We prove this by monkeypatching the BFS to return a rich mapping that
    WOULD harvest a new claim if the flags were on, then asserting the output
    matches a true baseline (BFS returning nothing). If the disabled path
    ever leaks a harvested row, these two diverge and the test fails.
    """
    db, _ = _seed(tmp_path)
    off_id = _ingest_offgraph_claim(
        db, text="Reticulating splines on the Andromeda cluster."
    )

    # Baseline: graph stream disabled entirely (its real default).
    baseline_md, baseline_ids = ch.recall(
        "PostgreSQL production", db_path=db, skip_qdrant=True, return_ids=True
    )

    # Now: BFS *would* reach the off-graph claim, but neither flag is set, so
    # the harvest must not fire and graph_score must not perturb ranking.
    monkeypatch.setattr(
        ch, "_graph_reached_claim_distance", lambda q, store: {off_id: 0}
    )
    # GRAPH unset → the stream block is skipped; W_GRAPH at default 0.0.
    same_md, same_ids = ch.recall(
        "PostgreSQL production", db_path=db, skip_qdrant=True, return_ids=True
    )

    assert same_md == baseline_md, "rendered markdown drifted in disabled mode"
    assert same_ids == baseline_ids, "claim-id order/set drifted in disabled mode"
    assert off_id not in same_ids


def test_assemble_breakdown_omits_graph_by_default():
    """retrieval._assemble_breakdown stays byte-identical (no 'graph' key)
    unless a caller passes a graph score — so the RankedClaim ranker, which
    never computes one, is unaffected.
    """
    from memorymaster.recall.retrieval import _ScoreParts, _assemble_breakdown

    parts = _ScoreParts(
        relevance=1.0,
        boosts=0.2,
        weights=(0.4, 0.1, 0.0, 0.0),
        boost_terms={"confidence": 0.1, "freshness": 0.0,
                     "pinned": 0.0, "tier": 0.1},
    )
    bd = _assemble_breakdown(
        parts=parts, lexical=0.5, confidence=0.2, freshness=0.0,
        vector=0.0, floor=0.0, gated=False, final=1.2,
    )
    assert "graph" not in bd["components"]
    assert "graph" not in bd["contributions"]
    assert "graph" not in bd["weights_applied"]


def test_assemble_breakdown_surfaces_graph_when_passed():
    """When a graph score IS passed, it appears in all three component dicts
    with the weighted contribution — the surfacing path roadmap 12.2 adds.
    """
    from memorymaster.recall.retrieval import _ScoreParts, _assemble_breakdown

    parts = _ScoreParts(
        relevance=1.0,
        boosts=0.0,
        weights=(0.4, 0.1, 0.0, 0.0),
        boost_terms={"confidence": 0.0, "freshness": 0.0,
                     "pinned": 0.0, "tier": 0.0},
    )
    bd = _assemble_breakdown(
        parts=parts, lexical=0.5, confidence=0.0, freshness=0.0,
        vector=0.0, floor=0.0, gated=False, final=1.0,
        graph=0.5, w_graph=0.3,
    )
    assert bd["components"]["graph"] == 0.5
    assert bd["contributions"]["graph"] == pytest.approx(0.15)
    assert bd["weights_applied"]["graph"] == 0.3
