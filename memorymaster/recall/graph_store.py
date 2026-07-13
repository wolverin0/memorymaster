"""Kuzu-backed graph store — MemoryMaster's 6th retrieval stream (roadmap 11.3).

The store persists a minimal two-node / one-edge schema::

    NODE TABLE Claim  (id INT64 PRIMARY KEY)
    NODE TABLE Entity (id INT64 PRIMARY KEY, kind STRING)
    REL  TABLE Mentions (FROM Claim TO Entity, created_at STRING)

The store is opt-in at both install time (``pip install memorymaster[graph]``)
and runtime (``MEMORYMASTER_RECALL_GRAPH=1``). When the Kuzu dependency is
missing or the on-disk DB is corrupt, :meth:`GraphStore.open` raises a
:class:`GraphStoreUnavailable` that callers — in particular
``context_hook.recall`` — silently swallow, falling back to the 5-stream
stack bit-for-bit (claim 11907).

Public API (kept deliberately small so the in-memory ``networkx`` fallback
at the bottom of this module matches it identically):

* :class:`GraphStore` — ``open`` / ``close`` / ``ingest_edges`` /
  ``neighbors`` / ``claims_for_entities``.
* :class:`GraphEdge` — frozen dataclass DTO.
* :class:`GraphStoreUnavailable` — raised by ``open`` when the backend is
  unusable.

Defensive-by-default contract (claim 11907): every public method returns
an empty result on any Kuzu error after logging a warning — the graph
stream never raises into the recall hot path.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "GraphEdge",
    "GraphStore",
    "GraphStoreUnavailable",
    "MENTIONS_REL",
]

# Rel-table name is a string constant so both backends + the backfill script
# can ``from graph_store import MENTIONS_REL`` without duplicating literals.
MENTIONS_REL = "Mentions"


class GraphStoreUnavailable(RuntimeError):
    """Raised by :meth:`GraphStore.open` when Kuzu is missing or the DB
    directory is unreadable. Callers are expected to catch this, log it,
    and disable the graph stream for the current recall() call.
    """


@dataclass(frozen=True)
class GraphEdge:
    """One ``Claim -[:Mentions]-> Entity`` edge.

    ``kind`` is reserved for future edge taxonomies (``"causes"``,
    ``"part_of"``, etc.) — today only ``"mentions"`` is populated so the
    edge table stays a single Kuzu REL TABLE.
    """

    claim_id: int
    entity_id: int
    kind: str = "mentions"


class GraphStore:
    """Kuzu-backed claim↔entity graph. One instance per process.

    The store is opened lazily on first use (:meth:`open`) and closed
    explicitly via :meth:`close`. It is safe to call ``close()`` more than
    once; it is NOT thread-safe — the recall hook runs single-threaded per
    call, which is the only contract we support today.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._db = None  # kuzu.Database — lazy
        self._conn = None  # kuzu.Connection — lazy

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def open(self) -> None:
        """Open an initialized Kuzu DB without creating or repairing schema.

        Raises :class:`GraphStoreUnavailable` when Kuzu is not importable,
        the DB is absent, or the required graph tables are unavailable. The
        message is
        intentionally generic — callers log it at DEBUG level because the
        graph stream is an opt-in enhancement, not a correctness layer.
        """
        if not self.path.exists():
            raise GraphStoreUnavailable(
                f"graph schema is not initialized at {self.path}; "
                "run the graph backfill initializer"
            )
        self._open_backend(create_parent=False)
        self._assert_schema_ready()

    def initialize(self) -> None:
        """Explicitly create or evolve the optional derived graph store."""
        self._open_backend(create_parent=True)
        self._ensure_schema()

    def _open_backend(self, *, create_parent: bool) -> None:
        if self._conn is not None:
            return
        try:
            import kuzu  # type: ignore
        except Exception as exc:
            raise GraphStoreUnavailable(
                f"kuzu import failed ({exc!r}); install memorymaster[graph]"
            ) from exc

        try:
            # Kuzu >= 0.10 uses a single database file, not a directory.
            # We still accept ``.kuzu`` paths for forward compatibility —
            # just make sure the PARENT directory exists and let Kuzu
            # create the file itself.
            if create_parent:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = kuzu.Database(str(self.path))
            self._conn = kuzu.Connection(self._db)
        except Exception as exc:
            # Cover both corrupt on-disk state and IO errors — treat as
            # "unavailable" so callers fall back cleanly.
            self._db = None
            self._conn = None
            raise GraphStoreUnavailable(
                f"kuzu open failed at {self.path}: {exc!r}"
            ) from exc


    def _assert_schema_ready(self) -> None:
        """Raise an actionable error when a read opens an incomplete store."""
        assert self._conn is not None
        try:
            self._conn.execute("MATCH (c:Claim) RETURN count(c)")
            self._conn.execute("MATCH (e:Entity) RETURN count(e)")
            self._conn.execute(
                f"MATCH (:Claim)-[m:{MENTIONS_REL}]->(:Entity) RETURN count(m)"
            )
        except Exception as exc:
            self.close()
            raise GraphStoreUnavailable(
                f"graph schema is not ready at {self.path}; "
                "run the graph backfill initializer"
            ) from exc

    def close(self) -> None:
        """Close the Kuzu connection + database. Safe to call twice."""
        # Kuzu has no explicit ``close`` on Connection; drop references and
        # let the C++ destructor run. Explicitly ``del`` so repeated
        # close/open cycles in tests don't leak file handles.
        try:
            if self._conn is not None:
                del self._conn
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            if self._db is not None:
                del self._db
        except Exception:  # pragma: no cover - defensive
            pass
        self._conn = None
        self._db = None

    def _ensure_schema(self) -> None:
        """Idempotent schema creation. Kuzu's IF NOT EXISTS makes this safe
        on every open().
        """
        assert self._conn is not None
        self._conn.execute(
            "CREATE NODE TABLE IF NOT EXISTS Claim (id INT64 PRIMARY KEY)"
        )
        self._conn.execute(
            "CREATE NODE TABLE IF NOT EXISTS Entity "
            "(id INT64 PRIMARY KEY, kind STRING)"
        )
        # Kuzu does NOT support UNIQUE on REL tables; we enforce idempotency
        # in ``ingest_edges`` via a pre-query (see below).
        self._conn.execute(
            f"CREATE REL TABLE IF NOT EXISTS {MENTIONS_REL} "
            "(FROM Claim TO Entity, created_at STRING)"
        )

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def ingest_edges(self, edges: list[GraphEdge]) -> int:
        """Upsert ``Claim`` + ``Entity`` nodes and a ``Mentions`` edge for
        every input. Returns the number of edges actually inserted
        (idempotent — duplicates are skipped).

        Implementation notes:
        * Nodes are ``MERGE``-ed first (Kuzu's ``CREATE`` is INSERT-only,
          so we use the ``MATCH ... WHEN NONE THEN CREATE`` pattern via two
          queries).
        * Edges are pre-queried per (claim, entity) pair so we don't write
          a duplicate. For the backfill volumes we care about (low tens of
          thousands) this is fine; a future optimisation is to batch via
          Kuzu's ``COPY FROM`` after collecting all pairs.
        """
        if self._conn is None:
            return 0
        if not edges:
            return 0

        created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        added = 0
        # Dedup the input list itself — the backfill script relies on this
        # to be cheap even when it re-ingests the same claim twice.
        seen: set[tuple[int, int]] = set()
        for edge in edges:
            key = (int(edge.claim_id), int(edge.entity_id))
            if key in seen:
                continue
            seen.add(key)
            try:
                if self._edge_exists(key[0], key[1]):
                    continue
                self._upsert_claim(key[0])
                self._upsert_entity(key[1], edge.kind)
                self._conn.execute(
                    f"MATCH (c:Claim {{id: $cid}}), (e:Entity {{id: $eid}}) "
                    f"CREATE (c)-[:{MENTIONS_REL} {{created_at: $ts}}]->(e)",
                    {"cid": key[0], "eid": key[1], "ts": created_at},
                )
                added += 1
            except Exception as exc:
                logger.debug("graph_store: ingest_edges skip (%s, %s): %s",
                             key[0], key[1], exc)
                continue
        return added

    def _edge_exists(self, claim_id: int, entity_id: int) -> bool:
        assert self._conn is not None
        result = self._conn.execute(
            f"MATCH (c:Claim {{id: $cid}})-[:{MENTIONS_REL}]->(e:Entity {{id: $eid}}) "
            "RETURN 1 LIMIT 1",
            {"cid": int(claim_id), "eid": int(entity_id)},
        )
        return bool(result.has_next())

    def _upsert_claim(self, claim_id: int) -> None:
        assert self._conn is not None
        exists = self._conn.execute(
            "MATCH (c:Claim {id: $cid}) RETURN 1 LIMIT 1",
            {"cid": int(claim_id)},
        )
        if not exists.has_next():
            self._conn.execute(
                "CREATE (:Claim {id: $cid})",
                {"cid": int(claim_id)},
            )

    def _upsert_entity(self, entity_id: int, kind: str) -> None:
        assert self._conn is not None
        exists = self._conn.execute(
            "MATCH (e:Entity {id: $eid}) RETURN 1 LIMIT 1",
            {"eid": int(entity_id)},
        )
        if not exists.has_next():
            self._conn.execute(
                "CREATE (:Entity {id: $eid, kind: $kind})",
                {"eid": int(entity_id), "kind": kind or "unknown"},
            )

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def neighbors(self, entity_ids: list[int], max_hops: int = 2) -> set[int]:
        """BFS from ``entity_ids`` through ``Entity <- Claim -> Entity``
        hops. Returns every entity_id reachable in up to ``max_hops``
        (including the seeds themselves).

        Each hop traverses ``(:Entity)<-[:Mentions]-(:Claim)-[:Mentions]->
        (:Entity)`` — two Kuzu edges are one logical "hop" in the
        claim-entity bipartite graph. ``max_hops=2`` therefore reaches
        entities two bridge-claims away, matching the Cognee example in
        the roadmap spec.

        Returns an empty set on any Kuzu error (claim 11907 silent-fail
        pattern).
        """
        if self._conn is None or not entity_ids:
            return set()
        if max_hops < 1:
            return {int(x) for x in entity_ids}

        frontier: set[int] = {int(x) for x in entity_ids}
        visited: set[int] = set(frontier)
        for _ in range(max_hops):
            if not frontier:
                break
            next_frontier: set[int] = set()
            try:
                result = self._conn.execute(
                    f"MATCH (src:Entity)<-[:{MENTIONS_REL}]-"
                    f"(c:Claim)-[:{MENTIONS_REL}]->(dst:Entity) "
                    "WHERE src.id IN $ids "
                    "RETURN DISTINCT dst.id",
                    {"ids": list(frontier)},
                )
                while result.has_next():
                    dst = int(result.get_next()[0])
                    if dst not in visited:
                        next_frontier.add(dst)
                        visited.add(dst)
            except Exception as exc:
                logger.debug("graph_store: neighbors hop failed: %s", exc)
                break
            frontier = next_frontier
        return visited

    def claims_for_entities(
        self,
        entity_ids: list[int],
        limit: int = 50,
    ) -> list[int]:
        """Return claim_ids that mention ANY of ``entity_ids`` (deduped).

        Ordered by graph-level created_at DESC — the closest to "recency"
        we can get without joining back to the claims table. Returns
        ``[]`` on any Kuzu error.
        """
        if self._conn is None or not entity_ids:
            return []
        try:
            result = self._conn.execute(
                f"MATCH (c:Claim)-[r:{MENTIONS_REL}]->(e:Entity) "
                "WHERE e.id IN $ids "
                "RETURN DISTINCT c.id, max(r.created_at) AS ts "
                "ORDER BY ts DESC "
                "LIMIT $lim",
                {"ids": [int(x) for x in entity_ids], "lim": int(limit)},
            )
            out: list[int] = []
            while result.has_next():
                row = result.get_next()
                out.append(int(row[0]))
            return out
        except Exception as exc:
            logger.debug("graph_store: claims_for_entities failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # distance-weighted reads (roadmap 12.1)
    # ------------------------------------------------------------------
    def _entity_hop_map(
        self, entity_ids: list[int], max_hops: int
    ) -> dict[int, int]:
        """Layer-by-layer BFS — return ``{entity_id: min_hop_from_seeds}``.

        Layer 0 = the seed entities themselves. Each subsequent layer is
        reached by traversing one bridge claim
        (``Entity <- Claim -> Entity``). Stops when ``max_hops`` is
        reached or the frontier is empty. Defensive: any Kuzu error
        terminates BFS early but returns whatever was already discovered
        (claim 11907 silent-fail pattern).
        """
        if not entity_ids:
            return {}
        hop_of: dict[int, int] = {int(x): 0 for x in entity_ids}
        if max_hops < 1:
            return hop_of
        frontier: set[int] = set(hop_of.keys())
        for hop in range(1, max_hops + 1):
            if not frontier:
                break
            next_frontier: set[int] = set()
            try:
                result = self._conn.execute(
                    f"MATCH (src:Entity)<-[:{MENTIONS_REL}]-"
                    f"(c:Claim)-[:{MENTIONS_REL}]->(dst:Entity) "
                    "WHERE src.id IN $ids "
                    "RETURN DISTINCT dst.id",
                    {"ids": list(frontier)},
                )
                while result.has_next():
                    dst = int(result.get_next()[0])
                    if dst not in hop_of:
                        hop_of[dst] = hop
                        next_frontier.add(dst)
            except Exception as exc:
                logger.debug("graph_store: hop %d BFS failed: %s", hop, exc)
                break
            frontier = next_frontier
        return hop_of

    def claims_for_entities_with_distance(
        self,
        entity_ids: list[int],
        max_hops: int = 2,
        limit: int = 50,
    ) -> list[tuple[int, int]]:
        """Return ``[(claim_id, min_hops_from_query_entities), ...]``.

        For every reachable claim, ``min_hops`` is the shortest BFS hop
        from any of ``entity_ids`` to an entity the claim mentions. A
        claim mentioning two entities at hops 1 AND 2 is emitted once at
        hop 1 (the smaller). Sorted by hops ASC, then by recency DESC
        (graph created_at). ``limit`` caps the total claim count after
        sorting.

        ``hops == 0`` means the claim mentions one of the query entities
        directly. The legacy :meth:`claims_for_entities` helper is kept
        for callers that don't need the hop count.

        Returns ``[]`` on any Kuzu error (claim 11907 silent-fail).
        """
        if self._conn is None or not entity_ids:
            return []
        try:
            hop_of = self._entity_hop_map(
                [int(x) for x in entity_ids], int(max_hops)
            )
            if not hop_of:
                return []
            # Pull every (claim, entity, ts) edge for the reached set,
            # then reduce to (claim → min(hop_of[entity])) in Python so
            # the dedupe rule stays portable across Kuzu versions.
            result = self._conn.execute(
                f"MATCH (c:Claim)-[r:{MENTIONS_REL}]->(e:Entity) "
                "WHERE e.id IN $ids "
                "RETURN c.id, e.id, r.created_at",
                {"ids": list(hop_of.keys())},
            )
            best_hop: dict[int, int] = {}
            best_ts: dict[int, str] = {}
            while result.has_next():
                row = result.get_next()
                cid = int(row[0])
                eid = int(row[1])
                ts = row[2] or ""
                hop = hop_of.get(eid)
                if hop is None:
                    continue
                prev_hop = best_hop.get(cid)
                if prev_hop is None or hop < prev_hop:
                    best_hop[cid] = hop
                    best_ts[cid] = ts
                elif hop == prev_hop and ts > best_ts.get(cid, ""):
                    best_ts[cid] = ts
            # Two-pass sort: by recency DESC first (secondary key), then
            # by hops ASC (stable primary). Python's sort is stable, so
            # this yields the desired (hops_asc, recency_desc) ordering.
            items = list(best_hop.items())
            items.sort(key=lambda kv: best_ts.get(kv[0], ""), reverse=True)
            items.sort(key=lambda kv: kv[1])
            return [(cid, hop) for cid, hop in items[: int(limit)]]
        except Exception as exc:
            logger.debug(
                "graph_store: claims_for_entities_with_distance failed: %s",
                exc,
            )
            return []


# ----------------------------------------------------------------------
# networkx fallback
# ----------------------------------------------------------------------
class _NetworkXGraphStore:
    """Pure-python in-memory graph store with the same public API.

    Used only when Kuzu is unavailable and the caller opts into the
    fallback via :func:`open_graph_store` ``allow_networkx=True``. The
    fallback does NOT persist — every process restart re-builds the
    graph via ``backfill_graph_store.py``. Documented in the
    accompanying artifact.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._claim_to_entities: dict[int, set[int]] = {}
        self._entity_to_claims: dict[int, set[int]] = {}
        self._entity_kind: dict[int, str] = {}

    def open(self) -> None:
        # in-memory — nothing to open
        return None

    def close(self) -> None:
        self._claim_to_entities.clear()
        self._entity_to_claims.clear()
        self._entity_kind.clear()

    def ingest_edges(self, edges: list[GraphEdge]) -> int:
        added = 0
        for e in edges:
            cid, eid = int(e.claim_id), int(e.entity_id)
            if eid in self._claim_to_entities.get(cid, ()):
                continue
            self._claim_to_entities.setdefault(cid, set()).add(eid)
            self._entity_to_claims.setdefault(eid, set()).add(cid)
            self._entity_kind.setdefault(eid, e.kind or "unknown")
            added += 1
        return added

    def neighbors(self, entity_ids: list[int], max_hops: int = 2) -> set[int]:
        if not entity_ids:
            return set()
        visited: set[int] = {int(x) for x in entity_ids}
        frontier: set[int] = set(visited)
        for _ in range(max(0, max_hops)):
            next_frontier: set[int] = set()
            for src in frontier:
                for cid in self._entity_to_claims.get(src, ()):
                    for dst in self._claim_to_entities.get(cid, ()):
                        if dst not in visited:
                            visited.add(dst)
                            next_frontier.add(dst)
            if not next_frontier:
                break
            frontier = next_frontier
        return visited

    def claims_for_entities(
        self, entity_ids: list[int], limit: int = 50
    ) -> list[int]:
        out: deque[int] = deque()
        seen: set[int] = set()
        for eid in entity_ids:
            for cid in self._entity_to_claims.get(int(eid), ()):
                if cid in seen:
                    continue
                seen.add(cid)
                out.append(cid)
                if len(out) >= limit:
                    return list(out)
        return list(out)

    def claims_for_entities_with_distance(
        self,
        entity_ids: list[int],
        max_hops: int = 2,
        limit: int = 50,
    ) -> list[tuple[int, int]]:
        """In-memory mirror of :meth:`GraphStore.claims_for_entities_with_distance`.

        BFS the bipartite graph layer-by-layer, track ``entity → min_hop``,
        then for each reached entity collect its mentioning claims. A
        claim's hop is the smallest hop of any entity it mentions.
        Sorted by ``(hops_asc, claim_id_desc)`` — the in-memory store
        has no graph timestamp, so claim_id (insertion order) is the
        recency proxy.
        """
        if not entity_ids:
            return []
        hop_of: dict[int, int] = {int(x): 0 for x in entity_ids}
        if max_hops >= 1:
            frontier: set[int] = set(hop_of.keys())
            for hop in range(1, int(max_hops) + 1):
                if not frontier:
                    break
                next_frontier: set[int] = set()
                for src in frontier:
                    for cid in self._entity_to_claims.get(src, ()):
                        for dst in self._claim_to_entities.get(cid, ()):
                            if dst not in hop_of:
                                hop_of[dst] = hop
                                next_frontier.add(dst)
                frontier = next_frontier
        best_hop: dict[int, int] = {}
        for eid, hop in hop_of.items():
            for cid in self._entity_to_claims.get(eid, ()):
                prev = best_hop.get(cid)
                if prev is None or hop < prev:
                    best_hop[cid] = hop
        items = sorted(best_hop.items(), key=lambda kv: (kv[1], -kv[0]))
        return [(cid, hop) for cid, hop in items[: int(limit)]]


def open_graph_store(
    path: Path | str,
    *,
    allow_networkx: bool = False,
    initialize: bool = False,
) -> "GraphStore | _NetworkXGraphStore":
    """Return an opened Kuzu store, or an explicitly allowed networkx fallback.

    ``initialize=True`` is reserved for administration/backfill paths. Normal
    recall opens and validates existing state without creating schema.

    Raises :class:`GraphStoreUnavailable` when Kuzu fails AND
    ``allow_networkx`` is False.
    """
    try:
        store = GraphStore(path)
        if initialize:
            store.initialize()
        else:
            store.open()
        return store
    except GraphStoreUnavailable:
        if not allow_networkx:
            raise
        logger.info("graph_store: falling back to networkx in-memory store")
        fb = _NetworkXGraphStore(path)
        fb.open()
        return fb
