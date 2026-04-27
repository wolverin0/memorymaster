"""Federated graphify — cross-project knowledge graph queries (v3.9.0 F7).

Ports graphify v0.5.0's ``merge-graphs`` concept into a MemoryMaster MCP
helper. Walks a list of project roots, looks for ``graphify-out/graph.json``
in each, merges nodes + edges with a per-node ``repo`` tag, and returns the
matching subset filtered by query.

Why this lives in MemoryMaster (not graphify): MemoryMaster already has the
MCP server + cross-project federated_query infrastructure, and the recall
hook is the natural consumer of "give me god-nodes for query X across all
my projects" answers. The actual graph build stays in graphify; we only
consume + merge the JSON output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = [
    "discover_graphify_projects",
    "load_graph",
    "merge_graphs",
    "federated_query",
]


GRAPHIFY_OUT = "graphify-out"
GRAPH_JSON = "graph.json"


def discover_graphify_projects(root: str | os.PathLike[str]) -> list[Path]:
    """Find every directory under ``root`` containing graphify-out/graph.json.

    Returns the project root paths (parents of ``graphify-out``), sorted by
    name for determinism.
    """
    rootp = Path(root)
    if not rootp.is_dir():
        return []
    out: list[Path] = []
    # Two-level scan is plenty: most setups have ~/projects/<name>/graphify-out
    for candidate in rootp.iterdir():
        if not candidate.is_dir():
            continue
        if (candidate / GRAPHIFY_OUT / GRAPH_JSON).is_file():
            out.append(candidate)
    return sorted(out, key=lambda p: p.name.lower())


def load_graph(project_root: str | os.PathLike[str]) -> dict:
    """Read ``<project_root>/graphify-out/graph.json``. Returns ``{}`` on miss."""
    p = Path(project_root) / GRAPHIFY_OUT / GRAPH_JSON
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _tag_nodes_with_repo(graph: dict, repo: str) -> list[dict]:
    """Return the graph's nodes with each node carrying a ``repo`` tag."""
    nodes = graph.get("nodes") or []
    out: list[dict] = []
    if not isinstance(nodes, list):
        return out
    for node in nodes:
        if not isinstance(node, dict):
            continue
        new_node = dict(node)
        new_node["repo"] = repo
        out.append(new_node)
    return out


def _tag_edges_with_repo(graph: dict, repo: str) -> list[dict]:
    edges = graph.get("edges") or []
    out: list[dict] = []
    if not isinstance(edges, list):
        return out
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        new_edge = dict(edge)
        new_edge["repo"] = repo
        out.append(new_edge)
    return out


def merge_graphs(project_roots: list[str | os.PathLike[str]]) -> dict:
    """Merge multiple graphify graphs into one big graph with repo-tagged nodes/edges.

    Nodes are deduped by ``(repo, id)`` to avoid collisions when two repos
    happen to use the same node id.
    """
    merged_nodes: list[dict] = []
    merged_edges: list[dict] = []
    seen_keys: set[tuple[str, object]] = set()
    repos: list[str] = []
    for root in project_roots:
        rootp = Path(root)
        repo = rootp.name
        repos.append(repo)
        graph = load_graph(rootp)
        if not graph:
            continue
        for node in _tag_nodes_with_repo(graph, repo):
            key = (repo, node.get("id"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged_nodes.append(node)
        merged_edges.extend(_tag_edges_with_repo(graph, repo))
    return {"nodes": merged_nodes, "edges": merged_edges, "repos": repos}


def federated_query(
    project_roots: list[str | os.PathLike[str]],
    query: str,
    *,
    limit: int = 20,
    repo_filter: str | None = None,
) -> list[dict]:
    """Substring-match nodes from a merged federated graph.

    Args:
        project_roots: list of project directories (each must have
            ``graphify-out/graph.json``).
        query: case-insensitive substring matched against node ``label`` /
            ``id`` / any string-typed field on the node.
        limit: max results.
        repo_filter: if set, only return nodes whose ``repo`` tag matches.

    Returns a list of node dicts with the ``repo`` tag preserved.
    """
    if not query or not query.strip():
        return []
    merged = merge_graphs(project_roots)
    needle = query.strip().lower()
    matches: list[dict] = []
    for node in merged.get("nodes", []):
        if repo_filter and node.get("repo") != repo_filter:
            continue
        for v in node.values():
            if isinstance(v, str) and needle in v.lower():
                matches.append(node)
                break
        if len(matches) >= limit:
            break
    return matches
