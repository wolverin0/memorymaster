"""Community detection over the entity knowledge graph.

Runs greedy modularity maximization (CNM — the networkx stand-in for a
Leiden-style pass, no extra native deps) over the ``entity_edges`` table
and returns size-ranked communities with their most-connected entities.

Design constraints (default-safe for a released package):
- **networkx is OPTIONAL.** Imported lazily; if absent we raise a
  ``CommunityDetectionUnavailable`` with an actionable install hint instead
  of breaking import of this module or any caller. Ship it via the
  ``memorymaster[graph]`` extra — never a core dependency.
- **Read-only.** Community IDs are made stable via deterministic size-rank
  ordering (sort by size DESC, then by the lexicographically smallest member
  entity name) rather than a persisted snapshot table, so this module NEVER
  writes to the DB and is safe to point at a live brain opened ``mode=ro``.
- **Zero cost when unused.** Nothing here runs unless a surface explicitly
  calls it (gated behind ``MEMORYMASTER_ENTITY_COMMUNITIES=1``).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from memorymaster.stores._storage_shared import connect_ro

logger = logging.getLogger(__name__)

INSTALL_HINT = (
    "networkx is not installed — entity community detection is unavailable. "
    "Install it with: pip install 'memorymaster[graph]' (or pip install networkx)."
)


class CommunityDetectionUnavailable(RuntimeError):
    """Raised when the optional networkx dependency is missing."""


def _import_networkx():
    try:
        import networkx as nx
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise CommunityDetectionUnavailable(INSTALL_HINT) from exc
    return nx


def _resolve_db_path(store: Any) -> str:
    """Accept a store object (anything with .db_path) or a plain path string."""
    return str(getattr(store, "db_path", store))


def _load_graph(db_path: str):
    """Build an undirected weighted entity graph from entity_edges (read-only).

    Parallel edges (same pair, different relation) have their weights summed.
    Entities with no edges are excluded — a community of one is noise.
    """
    nx = _import_networkx()
    graph = nx.Graph()
    conn = connect_ro(db_path)
    try:
        try:
            rows = conn.execute(
                """
                SELECT e.source_id AS src, e.target_id AS tgt, SUM(e.weight) AS w
                FROM entity_edges e
                GROUP BY e.source_id, e.target_id
                """
            ).fetchall()
            names = {
                r["id"]: r["name"]
                for r in conn.execute("SELECT id, name FROM entities").fetchall()
            }
        except sqlite3.OperationalError:
            # Entity tables were never created in this DB — empty graph.
            return graph, {}
    finally:
        conn.close()

    for row in rows:
        src, tgt = row["src"], row["tgt"]
        if src == tgt:
            continue
        weight = float(row["w"] or 1.0)
        if graph.has_edge(src, tgt):
            graph[src][tgt]["weight"] += weight
        else:
            graph.add_edge(src, tgt, weight=weight)
    return graph, names


def _rank_members(graph, members: list[str]) -> list[str]:
    """Order community members by weighted degree DESC (hub entities first)."""
    degree = graph.degree(members, weight="weight")
    return [node for node, _ in sorted(degree, key=lambda item: (-item[1], item[0]))]


def _detect(store: Any, *, min_size: int, top_entities: int) -> tuple[list[dict[str, Any]], float | None]:
    """Single detection pass: returns (size-ranked communities, modularity)."""
    nx = _import_networkx()
    graph, names = _load_graph(_resolve_db_path(store))
    if graph.number_of_edges() == 0:
        return [], None

    raw = nx.algorithms.community.greedy_modularity_communities(graph, weight="weight")
    modularity = float(nx.algorithms.community.modularity(graph, raw, weight="weight"))
    kept = [sorted(c) for c in raw if len(c) >= min_size]

    def _sort_key(members: list[str]) -> tuple[int, str]:
        member_names = sorted(names.get(m, m).lower() for m in members)
        return (-len(members), member_names[0] if member_names else "")

    kept.sort(key=_sort_key)
    result: list[dict[str, Any]] = []
    for community_id, members in enumerate(kept):
        ranked = _rank_members(graph, members)
        result.append(
            {
                "community_id": community_id,
                "size": len(members),
                "top_entities": [names.get(m, m) for m in ranked[:top_entities]],
            }
        )
    return result, modularity


def compute_communities(
    store: Any,
    *,
    min_size: int = 2,
    top_entities: int = 5,
) -> list[dict[str, Any]]:
    """Detect communities in the entity graph.

    Returns a list of ``{community_id, size, top_entities}`` dicts sorted by
    size DESC. IDs are STABLE across re-runs on similar data: rank 0 is always
    the biggest community, ties broken by the lexicographically smallest
    member entity name — so as long as the big clusters stay big, their IDs
    don't move (deterministic size-rank ordering; no snapshot writes needed).

    Raises ``CommunityDetectionUnavailable`` if networkx is not installed.
    Never writes to the DB.
    """
    communities, _ = _detect(store, min_size=min_size, top_entities=top_entities)
    return communities


def community_summary(store: Any, *, top_n: int = 5) -> dict[str, Any]:
    """Compact summary for entity_stats surfaces: count, modularity, top N.

    Raises ``CommunityDetectionUnavailable`` if networkx is missing — callers
    surface the message instead of crashing.
    """
    communities, modularity = _detect(store, min_size=2, top_entities=top_n)
    return {
        "count": len(communities),
        "modularity": round(modularity, 4) if modularity is not None else None,
        "top": communities[:top_n],
    }
