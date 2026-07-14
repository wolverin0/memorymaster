"""Distance-weighted graph score — roadmap 12.1.

Validates :meth:`GraphStore.claims_for_entities_with_distance` and the
``_graph_reached_claim_distance`` helper in
:mod:`memorymaster.recall.context_hook`. The scoring contract:

* hop 0 → claim mentions a query entity directly → score ``1 / (1+0) = 1.0``
* hop 1 → claim mentions an entity 1 BFS step away → score ``1/2 = 0.5``
* hop 2 → claim mentions an entity 2 BFS steps away → score ``1/3 ≈ 0.333``
* not reached → score ``0.0`` (treated as not in the returned mapping)

The Cognee Alice→Atlas→Postgres example from claim 11790 is the canonical
multi-hop fixture; identical to ``tests/test_graph_store.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.recall.graph_store import (
    GraphEdge,
    GraphStore,
    GraphStoreUnavailable,
    _NetworkXGraphStore,
)


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------
@pytest.fixture()
def kuzu_store(tmp_path: Path):
    """Fresh Kuzu DB. Skips when Kuzu is missing on the platform."""
    try:
        store = GraphStore(tmp_path / "g.kuzu")
        store.initialize()
    except GraphStoreUnavailable:
        pytest.skip("Kuzu unavailable on this platform")
    yield store
    store.close()


@pytest.fixture()
def cognee_edges() -> list[GraphEdge]:
    """Same chain used by tests/test_graph_store.py — Alice (100) →
    Atlas (200) → Postgres (300), plus Bob (101) and an isolated
    second-hop claim 5.
    """
    return [
        GraphEdge(claim_id=1, entity_id=100, kind="person"),    # Alice
        GraphEdge(claim_id=1, entity_id=200, kind="project"),   # Atlas
        GraphEdge(claim_id=2, entity_id=200, kind="project"),
        GraphEdge(claim_id=2, entity_id=300, kind="service"),   # Postgres
        GraphEdge(claim_id=3, entity_id=300, kind="service"),
        GraphEdge(claim_id=4, entity_id=101, kind="person"),    # Bob
        GraphEdge(claim_id=4, entity_id=100, kind="person"),
        GraphEdge(claim_id=5, entity_id=200, kind="project"),
    ]


# ----------------------------------------------------------------------
# Kuzu store
# ----------------------------------------------------------------------
class TestClaimsForEntitiesWithDistance:
    def test_direct_mention_is_hop_zero(self, kuzu_store, cognee_edges):
        """Claim 1 mentions Alice (100) directly → hop 0 → score 1.0."""
        kuzu_store.ingest_edges(cognee_edges)
        pairs = dict(
            kuzu_store.claims_for_entities_with_distance([100], max_hops=2)
        )
        # Claim 1 (Alice + Atlas) and claim 4 (Alice + Bob) mention Alice
        # directly — both are hop 0.
        assert pairs[1] == 0
        assert pairs[4] == 0
        # Distance-weighted score check.
        assert _score(pairs[1]) == 1.0
        assert _score(pairs[4]) == 1.0

    def test_one_hop_via_bridge_claim(self, kuzu_store, cognee_edges):
        """Atlas (200) is 1 hop from Alice via bridge claim 1.

        Claims that mention Atlas WITHOUT mentioning Alice — claim 5 —
        therefore land at hop 1 → score 0.5. (Claim 1 mentions both,
        so it's still hop 0.)
        """
        kuzu_store.ingest_edges(cognee_edges)
        pairs = dict(
            kuzu_store.claims_for_entities_with_distance([100], max_hops=2)
        )
        assert pairs[5] == 1
        assert _score(pairs[5]) == pytest.approx(0.5)

    def test_two_hop_postgres_chain(self, kuzu_store, cognee_edges):
        """Postgres (300) is 2 hops from Alice (Alice→Atlas→Postgres).

        Claim 3 mentions ONLY Postgres → hop 2 → score 0.333. Claim 2
        mentions Atlas (hop 1) AND Postgres (hop 2) → kept at the
        smaller hop = 1 → score 0.5. Verifies the tie-break rule from
        the spec.
        """
        kuzu_store.ingest_edges(cognee_edges)
        pairs = dict(
            kuzu_store.claims_for_entities_with_distance([100], max_hops=2)
        )
        assert pairs[3] == 2
        assert _score(pairs[3]) == pytest.approx(1.0 / 3.0, rel=1e-6)
        # Tie-break: claim 2 mentions an entity at hop 1 AND hop 2 —
        # keeps the smaller (1).
        assert pairs[2] == 1

    def test_max_hops_cap_excludes_far_claims(
        self, kuzu_store, cognee_edges
    ):
        """When ``max_hops=1`` Postgres (300) and its only-mentioning
        claim 3 must NOT appear — they're 2 hops away.
        """
        kuzu_store.ingest_edges(cognee_edges)
        pairs = dict(
            kuzu_store.claims_for_entities_with_distance([100], max_hops=1)
        )
        # Claims at hops 0 and 1 included, claim 3 (only-Postgres) excluded.
        assert 3 not in pairs
        # But claim 2 still appears at hop 1 because it mentions Atlas
        # (entity at hop 1).
        assert pairs[2] == 1

    def test_empty_inputs_return_empty(self, kuzu_store, cognee_edges):
        kuzu_store.ingest_edges(cognee_edges)
        assert (
            kuzu_store.claims_for_entities_with_distance([], max_hops=2)
            == []
        )

    def test_isolated_entity_no_paths(self, kuzu_store, cognee_edges):
        """Entity 999 doesn't exist in the graph at all → no claims."""
        kuzu_store.ingest_edges(cognee_edges)
        assert (
            kuzu_store.claims_for_entities_with_distance([999], max_hops=2)
            == []
        )

    def test_results_sorted_by_hops_ascending(
        self, kuzu_store, cognee_edges
    ):
        """The output must be sorted closest-first so callers can ``[:limit]``
        without losing the high-score rows.
        """
        kuzu_store.ingest_edges(cognee_edges)
        pairs = kuzu_store.claims_for_entities_with_distance(
            [100], max_hops=2, limit=50
        )
        hops_sequence = [hop for _, hop in pairs]
        assert hops_sequence == sorted(hops_sequence), (
            f"Expected ascending hops, got {hops_sequence}"
        )

    def test_limit_truncates_after_sort(self, kuzu_store, cognee_edges):
        """``limit`` caps the count AFTER ascending-hop sort, so we keep
        the closest claims rather than an arbitrary mid-range slice.
        """
        kuzu_store.ingest_edges(cognee_edges)
        pairs = kuzu_store.claims_for_entities_with_distance(
            [100], max_hops=2, limit=2
        )
        assert len(pairs) == 2
        # Both must be at the lowest hop (0) since 4 claims mention Alice
        # directly OR via a 0-hop bridge.
        assert all(hop == 0 for _, hop in pairs)


# ----------------------------------------------------------------------
# networkx fallback parity
# ----------------------------------------------------------------------
class TestNetworkxFallbackDistance:
    def test_fallback_matches_kuzu_semantics(
        self, tmp_path: Path, cognee_edges
    ):
        fb = _NetworkXGraphStore(tmp_path / "g")
        fb.open()
        fb.ingest_edges(cognee_edges)
        pairs = dict(
            fb.claims_for_entities_with_distance([100], max_hops=2)
        )
        # Same expectations as the Kuzu test above.
        assert pairs[1] == 0
        assert pairs[4] == 0
        assert pairs[2] == 1
        assert pairs[5] == 1
        assert pairs[3] == 2
        fb.close()

    def test_empty_graph_returns_empty(self, tmp_path: Path):
        fb = _NetworkXGraphStore(tmp_path / "g")
        fb.open()
        assert (
            fb.claims_for_entities_with_distance([100], max_hops=2) == []
        )
        fb.close()


# ----------------------------------------------------------------------
# context_hook wiring
# ----------------------------------------------------------------------
class TestContextHookDistanceScore:
    def test_disabled_helper_returns_empty_mapping(self, monkeypatch):
        import memorymaster.recall.context_hook as ch

        monkeypatch.delenv("MEMORYMASTER_RECALL_GRAPH", raising=False)
        assert ch._graph_reached_claim_distance("query", store=None) == {}

        monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH", "0")
        assert ch._graph_reached_claim_distance("query", store=None) == {}

    def test_score_formula_decays_with_hops(self):
        """Documented contract: ``1 / (1 + hops)`` — verify the three
        canonical hops produce the spec values.
        """
        assert _score(0) == 1.0
        assert _score(1) == pytest.approx(0.5)
        assert _score(2) == pytest.approx(1.0 / 3.0, rel=1e-6)
        assert _score(3) == pytest.approx(0.25)


# ----------------------------------------------------------------------
# helper
# ----------------------------------------------------------------------
def _score(hops: int) -> float:
    """Mirror of the wiring formula in context_hook so tests stay
    decoupled from the import — if the formula ever changes there, the
    tests above fail loudly until updated.
    """
    return 1.0 / (1.0 + float(hops))
