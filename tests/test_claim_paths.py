"""Tests for claim-edge path queries (provenance / conflict / impact).

Each test anchors on the REQUIREMENT the feature exists to satisfy — an agent
asking relational questions over the claim_links graph — not on incidental
implementation details. The confidence roll-up is WEAKEST-LINK semantics
(minimum claim confidence along the path); see service.query_claim_paths.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MAX_CLAIM_PATH_HOPS, MemoryService


def _case_db(prefix: str) -> Path:
    os.makedirs(".tmp_cases", exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def _make_service() -> MemoryService:
    svc = MemoryService(_case_db("claim-paths"), workspace_root=Path.cwd())
    svc.init_db()
    return svc


def _ingest(svc: MemoryService, text: str, *, confidence: float = 0.9,
            source_agent: str | None = None, visibility: str = "public") -> int:
    claim = svc.ingest(
        text=text,
        citations=[CitationInput(source="test", locator="loc")],
        confidence=confidence,
        source_agent=source_agent,
        visibility=visibility,
    )
    return claim.id


def _confirm(svc: MemoryService, claim_id: int) -> None:
    claim = svc.store.get_claim(claim_id, include_citations=False)
    svc.store.apply_status_transition(
        claim, to_status="confirmed", reason="test", event_type="validator"
    )


def _set_status(svc: MemoryService, claim_id: int, status: str, event_type: str) -> None:
    claim = svc.store.get_claim(claim_id, include_citations=False)
    svc.store.apply_status_transition(
        claim, to_status=status, reason="test", event_type=event_type
    )


class TestDirectionFilters:
    def test_provenance_returns_inbound_only(self):
        """direction='in' answers 'what led to X?' — only claims pointing AT X."""
        svc = _make_service()
        x = _ingest(svc, "X target")
        cause = _ingest(svc, "cause of X")
        consequence = _ingest(svc, "depends on X")
        svc.add_claim_link(cause, x, "derived_from")   # cause -> x
        svc.add_claim_link(consequence, x, "depends_on")  # consequence -> x
        for cid in (x, cause, consequence):
            _confirm(svc, cid)

        rows = svc.query_claim_paths(x, direction="in")
        ids = {r["claim"]["id"] for r in rows}
        # Both edges point AT x, so both are inbound provenance.
        assert ids == {cause, consequence}

    def test_impact_returns_outbound_only(self):
        """direction='out' answers 'what does X point to?' — outbound edges."""
        svc = _make_service()
        x = _ingest(svc, "X source")
        downstream = _ingest(svc, "X derives from this")
        svc.add_claim_link(x, downstream, "derived_from")  # x -> downstream
        for cid in (x, downstream):
            _confirm(svc, cid)

        out_rows = svc.query_claim_paths(x, direction="out")
        assert {r["claim"]["id"] for r in out_rows} == {downstream}
        # The reverse direction from x has no inbound edges.
        assert svc.query_claim_paths(x, direction="in") == []

    def test_both_returns_all_neighbors(self):
        svc = _make_service()
        x = _ingest(svc, "X")
        a = _ingest(svc, "A -> X")
        b = _ingest(svc, "X -> B")
        svc.add_claim_link(a, x, "supports")
        svc.add_claim_link(x, b, "supports")
        for cid in (x, a, b):
            _confirm(svc, cid)

        rows = svc.query_claim_paths(x, direction="both")
        assert {r["claim"]["id"] for r in rows} == {a, b}


class TestEdgeTypeFilter:
    def test_conflict_query_filters_by_edge_type(self):
        """edge_type='contradicts' answers 'what contradicts X?' and excludes others."""
        svc = _make_service()
        x = _ingest(svc, "X")
        contra = _ingest(svc, "contradicts X")
        related = _ingest(svc, "merely relates to X")
        svc.add_claim_link(contra, x, "contradicts")
        svc.add_claim_link(related, x, "relates_to")
        for cid in (x, contra, related):
            _confirm(svc, cid)

        rows = svc.query_claim_paths(x, edge_type="contradicts", direction="in")
        ids = {r["claim"]["id"] for r in rows}
        assert ids == {contra}
        assert related not in ids
        assert all(r["edge_chain"] == ["contradicts"] for r in rows)


class TestMaxHops:
    def test_max_hops_bounds_traversal_depth(self):
        """A 1-hop query must not reach a 2-hops-away claim."""
        svc = _make_service()
        a = _ingest(svc, "A")
        b = _ingest(svc, "B")
        c = _ingest(svc, "C")
        svc.add_claim_link(b, a, "derived_from")  # b -> a
        svc.add_claim_link(c, b, "derived_from")  # c -> b
        for cid in (a, b, c):
            _confirm(svc, cid)

        one_hop = {r["claim"]["id"] for r in svc.query_claim_paths(a, direction="in", max_hops=1)}
        assert one_hop == {b}
        two_hop = {r["claim"]["id"] for r in svc.query_claim_paths(a, direction="in", max_hops=2)}
        assert two_hop == {b, c}

    def test_max_hops_clamped_to_ceiling(self):
        """An absurd max_hops must be clamped, not honored, to bound fan-out."""
        svc = _make_service()
        # Build a chain longer than the ceiling: n0 <- n1 <- ... <- n7
        ids = [_ingest(svc, f"node {i}") for i in range(MAX_CLAIM_PATH_HOPS + 3)]
        for cid in ids:
            _confirm(svc, cid)
        for i in range(len(ids) - 1):
            svc.add_claim_link(ids[i + 1], ids[i], "derived_from")  # i+1 -> i

        rows = svc.query_claim_paths(ids[0], direction="in", max_hops=999)
        max_depth = max(r["depth"] for r in rows)
        assert max_depth <= MAX_CLAIM_PATH_HOPS


class TestCircularSafety:
    def test_cycle_does_not_loop_forever(self):
        """A <-> B cycle must terminate via the BFS visited-set."""
        svc = _make_service()
        a = _ingest(svc, "A")
        b = _ingest(svc, "B")
        svc.add_claim_link(a, b, "relates_to")
        svc.add_claim_link(b, a, "relates_to")
        for cid in (a, b):
            _confirm(svc, cid)

        rows = svc.query_claim_paths(a, direction="both", max_hops=5)
        # B is reached exactly once; A (the start) is never re-emitted.
        ids = [r["claim"]["id"] for r in rows]
        assert ids.count(b) == 1
        assert a not in ids


class TestStatusFiltering:
    def test_stale_excluded_by_default_included_on_flag(self):
        svc = _make_service()
        x = _ingest(svc, "X")
        stale_cause = _ingest(svc, "stale cause")
        svc.add_claim_link(stale_cause, x, "derived_from")
        _confirm(svc, x)
        _set_status(svc, stale_cause, "stale", "decay")

        assert svc.query_claim_paths(x, direction="in") == []
        included = svc.query_claim_paths(x, direction="in", include_stale=True)
        assert {r["claim"]["id"] for r in included} == {stale_cause}

    def test_conflicted_excluded_by_default_included_on_flag(self):
        svc = _make_service()
        x = _ingest(svc, "X")
        conflicted = _ingest(svc, "conflicted neighbor")
        svc.add_claim_link(conflicted, x, "contradicts")
        _confirm(svc, x)
        _set_status(svc, conflicted, "conflicted", "validator")

        assert svc.query_claim_paths(x, direction="in") == []
        included = svc.query_claim_paths(x, direction="in", include_conflicted=True)
        assert {r["claim"]["id"] for r in included} == {conflicted}


class TestVisibility:
    def test_private_claim_of_other_agent_not_returned(self):
        """Per-agent visibility: agentB must not see agentA's private claim."""
        svc = _make_service()
        x = _ingest(svc, "X", source_agent="agentB")
        private_a = _ingest(svc, "secret", source_agent="agentA", visibility="private")
        public_a = _ingest(svc, "public", source_agent="agentA", visibility="public")
        svc.add_claim_link(private_a, x, "derived_from")
        svc.add_claim_link(public_a, x, "derived_from")
        for cid in (x, private_a, public_a):
            _confirm(svc, cid)

        rows = svc.query_claim_paths(x, direction="in", requesting_agent="agentB")
        ids = {r["claim"]["id"] for r in rows}
        assert private_a not in ids
        assert public_a in ids

        # agentA sees its own private claim.
        own = svc.query_claim_paths(x, direction="in", requesting_agent="agentA")
        assert private_a in {r["claim"]["id"] for r in own}


class TestOrphanAndConfidence:
    def test_unknown_claim_returns_empty(self):
        svc = _make_service()
        assert svc.query_claim_paths(999_999) == []

    def test_orphan_claim_with_no_links_returns_empty(self):
        svc = _make_service()
        lonely = _ingest(svc, "no links")
        _confirm(svc, lonely)
        assert svc.query_claim_paths(lonely) == []

    def test_weakest_link_confidence_rollup(self):
        """path_confidence is the MINIMUM claim confidence along the path."""
        svc = _make_service()
        x = _ingest(svc, "X", confidence=0.9)
        mid = _ingest(svc, "mid", confidence=0.4)   # the weak link
        far = _ingest(svc, "far", confidence=0.8)
        svc.add_claim_link(mid, x, "derived_from")   # mid -> x
        svc.add_claim_link(far, mid, "derived_from")  # far -> mid
        for cid in (x, mid, far):
            _confirm(svc, cid)

        rows = {r["claim"]["id"]: r for r in svc.query_claim_paths(x, direction="in", max_hops=2)}
        # Path to mid: [x=0.9, mid=0.4] -> min 0.4
        assert rows[mid]["path_confidence"] == pytest.approx(0.4)
        # Path to far: [x=0.9, mid=0.4, far=0.8] -> still 0.4 (weakest link)
        assert rows[far]["path_confidence"] == pytest.approx(0.4)

    def test_human_id_is_accepted(self):
        svc = _make_service()
        x = _ingest(svc, "X")
        cause = _ingest(svc, "cause")
        svc.add_claim_link(cause, x, "derived_from")
        for cid in (x, cause):
            _confirm(svc, cid)
        x_claim = svc.store.get_claim(x, include_citations=False)
        assert x_claim.human_id  # human_id assigned on ingest

        rows = svc.query_claim_paths(x_claim.human_id, direction="in")
        assert {r["claim"]["id"] for r in rows} == {cause}


class TestMcpTool:
    def test_mcp_query_claim_paths_returns_paths(self, tmp_path, monkeypatch):
        from memorymaster.surfaces import mcp_server

        db = tmp_path / "mcp-paths.db"
        svc = MemoryService(db, workspace_root=tmp_path)
        svc.init_db()
        x = _ingest(svc, "X")
        cause = _ingest(svc, "cause of X")
        svc.add_claim_link(cause, x, "derived_from")
        for cid in (x, cause):
            _confirm(svc, cid)

        result = mcp_server.query_claim_paths(
            claim_id=str(x), db=str(db), workspace=str(tmp_path), direction="in"
        )
        assert result["ok"] is True
        assert result["rows"] == 1
        assert result["paths"][0]["claim"]["id"] == cause
        assert result["paths"][0]["edge_chain"] == ["derived_from"]


class TestCli:
    def test_cli_query_paths_json(self, tmp_path, capsys):
        import json as _json

        from memorymaster.surfaces.cli import main

        db = tmp_path / "cli-paths.db"
        svc = MemoryService(db, workspace_root=tmp_path)
        svc.init_db()
        x = _ingest(svc, "X")
        cause = _ingest(svc, "cause of X")
        svc.add_claim_link(cause, x, "derived_from")
        for cid in (x, cause):
            _confirm(svc, cid)

        rc = main(["--json", "--db", str(db), "query-paths", "--claim-id", str(x), "--direction", "in"])
        assert rc == 0
        out = _json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["data"]["rows"] == 1
        assert out["data"]["paths"][0]["claim"]["id"] == cause
