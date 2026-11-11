"""Tests for v3.9.0 F7 — federated graphify MCP helpers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.bridges.federated_graphify import (
    discover_graphify_projects,
    federated_query,
    load_graph,
    merge_graphs,
)


def _make_project(root: Path, name: str, nodes: list[dict], edges: list[dict] | None = None) -> Path:
    proj = root / name
    out = proj / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    payload = {"nodes": nodes, "edges": edges or []}
    (out / "graph.json").write_text(json.dumps(payload), encoding="utf-8")
    return proj


def test_discover_returns_empty_for_missing_root(tmp_path):
    assert discover_graphify_projects(tmp_path / "does-not-exist") == []


def test_discover_finds_projects_with_graph(tmp_path):
    _make_project(tmp_path, "alpha", [{"id": "a1"}])
    _make_project(tmp_path, "beta", [{"id": "b1"}])
    (tmp_path / "no-graph").mkdir()
    out = discover_graphify_projects(tmp_path)
    names = [p.name for p in out]
    assert names == ["alpha", "beta"]


def test_load_graph_returns_empty_for_missing_file(tmp_path):
    assert load_graph(tmp_path / "no-graph") == {}


def test_load_graph_returns_empty_for_malformed_json(tmp_path):
    proj = tmp_path / "broken"
    out = proj / "graphify-out"
    out.mkdir(parents=True)
    (out / "graph.json").write_text("not valid json{", encoding="utf-8")
    assert load_graph(proj) == {}


def test_load_graph_returns_dict(tmp_path):
    proj = _make_project(tmp_path, "alpha", [{"id": "a1", "label": "Alpha One"}])
    g = load_graph(proj)
    assert g["nodes"][0]["id"] == "a1"


def test_merge_graphs_tags_nodes_with_repo(tmp_path):
    p1 = _make_project(tmp_path, "alpha", [{"id": "a1", "label": "Alpha"}])
    p2 = _make_project(tmp_path, "beta", [{"id": "b1", "label": "Beta"}])
    merged = merge_graphs([p1, p2])
    repos = {n["repo"] for n in merged["nodes"]}
    assert repos == {"alpha", "beta"}
    assert merged["repos"] == ["alpha", "beta"]


def test_merge_graphs_dedupes_within_repo(tmp_path):
    """Two nodes with the same id from the SAME repo collapse to one."""
    p1 = _make_project(
        tmp_path,
        "alpha",
        [{"id": "x", "label": "First"}, {"id": "x", "label": "Duplicate"}],
    )
    merged = merge_graphs([p1])
    nodes_with_id_x = [n for n in merged["nodes"] if n["id"] == "x"]
    assert len(nodes_with_id_x) == 1


def test_merge_graphs_keeps_collisions_across_repos(tmp_path):
    """Two repos with the same node id are NOT deduped — repo tag is the discriminator."""
    p1 = _make_project(tmp_path, "alpha", [{"id": "shared", "label": "A"}])
    p2 = _make_project(tmp_path, "beta", [{"id": "shared", "label": "B"}])
    merged = merge_graphs([p1, p2])
    shared = [n for n in merged["nodes"] if n["id"] == "shared"]
    assert len(shared) == 2
    assert {n["repo"] for n in shared} == {"alpha", "beta"}


def test_merge_graphs_handles_missing_project_silently(tmp_path):
    p1 = _make_project(tmp_path, "alpha", [{"id": "a1"}])
    merged = merge_graphs([p1, tmp_path / "does-not-exist"])
    # Both repos are recorded, but only alpha contributes nodes
    assert merged["repos"] == ["alpha", "does-not-exist"]
    assert all(n["repo"] == "alpha" for n in merged["nodes"])


def test_federated_query_substring_match(tmp_path):
    p1 = _make_project(
        tmp_path,
        "alpha",
        [
            {"id": "n1", "label": "MemPalace adapter"},
            {"id": "n2", "label": "Other thing"},
        ],
    )
    p2 = _make_project(
        tmp_path,
        "beta",
        [{"id": "m1", "label": "Wrapper around MemPalace API"}],
    )
    hits = federated_query([p1, p2], "mempalace")
    labels = {h["label"] for h in hits}
    assert labels == {"MemPalace adapter", "Wrapper around MemPalace API"}


def test_federated_query_repo_filter(tmp_path):
    p1 = _make_project(tmp_path, "alpha", [{"id": "n1", "label": "MemPalace"}])
    p2 = _make_project(tmp_path, "beta", [{"id": "m1", "label": "MemPalace"}])
    hits = federated_query([p1, p2], "mempalace", repo_filter="alpha")
    assert len(hits) == 1
    assert hits[0]["repo"] == "alpha"


def test_federated_query_returns_empty_for_empty_query(tmp_path):
    p1 = _make_project(tmp_path, "alpha", [{"id": "n1", "label": "x"}])
    assert federated_query([p1], "") == []
    assert federated_query([p1], "   ") == []


def test_federated_query_respects_limit(tmp_path):
    p1 = _make_project(
        tmp_path,
        "alpha",
        [{"id": f"n{i}", "label": "MemPalace"} for i in range(10)],
    )
    hits = federated_query([p1], "mempalace", limit=3)
    assert len(hits) == 3
