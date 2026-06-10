"""Backfill the Kuzu graph store from the live SQLite ``claims`` +
``entity_aliases`` tables (roadmap 11.3).

Reads every (claim_id, entity_id) link directly from the claims table's
``entity_id`` column — that's the canonical per-claim entity assignment
written by :mod:`memorymaster.knowledge.entity_registry` during ingest. Also mines
``entity_aliases`` rows whose ``original_form`` appears in a claim's
``subject``/``text``, so claims that mention multiple entities (legacy
claims didn't have the per-claim ``entity_id`` populated for every
reference) get backfilled too.

The output graph lives in a SEPARATE Kuzu directory (default
``~/.memorymaster/graph.kuzu``) — the live SQLite DB is NEVER mutated.

Idempotency: each run re-queries existing edges before inserting, so
re-running the script produces zero duplicates.

Flags::

    --db PATH              SQLite DB to read (default: memorymaster.db)
    --graph-path PATH      Kuzu DB directory (default: ~/.memorymaster/graph.kuzu)
    --dry-run              Count edges without writing
    --limit N              Process only the first N claims
    --progress-every N     Log every N claims (default: 500)
    --allow-networkx       Use in-memory networkx fallback when Kuzu is
                           unavailable. Useful in tests; NOT persistent.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from memorymaster.graph_store import (  # noqa: E402
    GraphEdge,
    GraphStoreUnavailable,
    open_graph_store,
)

logger = logging.getLogger("backfill_graph_store")


def _default_graph_path() -> Path:
    return Path.home() / ".memorymaster" / "graph.kuzu"


def _iter_claim_entity_edges(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
):
    """Yield ``GraphEdge`` tuples for every (claim, entity) pair.

    Two sources:
    1. ``claims.entity_id`` — the canonical per-claim subject entity.
    2. ``entity_aliases.original_form`` substring match in claim text —
       catches multi-entity mentions the canonical column misses.

    Source #1 runs first with a hard ``LIMIT`` when requested, then #2
    augments each claim with aliases found in its text.
    """
    lim_clause = f"LIMIT {int(limit)}" if limit else ""

    # Source 1: claims.entity_id → 1 edge per claim
    cur = conn.execute(
        "SELECT id, entity_id, subject, text "
        "FROM claims "
        "WHERE entity_id IS NOT NULL AND entity_id > 0 "
        "  AND status != 'archived' "
        f"ORDER BY id {lim_clause}"
    )
    claim_rows = cur.fetchall()
    logger.info("source-1: %d claims with entity_id", len(claim_rows))

    # Pre-load entity_aliases once — 33k rows is a few MB, trivially
    # cached. We want a fast substring-containment check against every
    # claim's text. Group by original_form so short aliases like "git"
    # don't spam.
    alias_cur = conn.execute(
        "SELECT DISTINCT entity_id, original_form "
        "FROM entity_aliases "
        "WHERE original_form IS NOT NULL AND length(original_form) >= 3"
    )
    aliases: list[tuple[int, str]] = [
        (int(eid), form) for eid, form in alias_cur.fetchall() if form
    ]
    logger.info("source-2: %d alias candidates preloaded", len(aliases))

    # Kind lookup — we want to stamp Entity.kind during ingest.
    kind_cur = conn.execute(
        "SELECT id, COALESCE(entity_type, 'unknown') FROM entities"
    )
    kind_by_entity: dict[int, str] = {
        int(row[0]): (row[1] or "unknown") for row in kind_cur.fetchall()
    }

    for cid, eid, subject, text in claim_rows:
        cid_i, eid_i = int(cid), int(eid)
        # Primary edge — claim → its subject entity.
        yield GraphEdge(
            claim_id=cid_i,
            entity_id=eid_i,
            kind=kind_by_entity.get(eid_i, "unknown"),
        )

        # Mentions discovered in text — skip when subject+text is empty.
        haystack = f"{subject or ''} {text or ''}".lower()
        if not haystack.strip():
            continue
        # Limit per-claim mentions to keep the backfill bounded. 8 is the
        # same cap the entity fanout uses in the recall hook (claim 11830).
        added_here = 0
        seen_aliases: set[int] = {eid_i}
        for alias_eid, form in aliases:
            if alias_eid in seen_aliases:
                continue
            if form.lower() in haystack:
                seen_aliases.add(alias_eid)
                yield GraphEdge(
                    claim_id=cid_i,
                    entity_id=alias_eid,
                    kind=kind_by_entity.get(alias_eid, "unknown"),
                )
                added_here += 1
                if added_here >= 8:
                    break


def run_backfill(
    db_path: Path,
    graph_path: Path,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    progress_every: int = 500,
    allow_networkx: bool = False,
) -> dict:
    """Execute the backfill. Returns a summary dict (printed in ``main``)."""
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite DB missing: {db_path}")

    store = None
    if not dry_run:
        try:
            store = open_graph_store(graph_path, allow_networkx=allow_networkx)
        except GraphStoreUnavailable as exc:
            logger.error(
                "graph_store unavailable: %s — pass --allow-networkx for "
                "an in-memory fallback, or install memorymaster[graph].",
                exc,
            )
            raise

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA query_only = ON")  # read-only enforcement
    start = time.perf_counter()
    buffer: list[GraphEdge] = []
    total_edges = 0
    written = 0
    last_claim_id: int | None = None
    processed_claims = 0

    BATCH = 200

    try:
        for edge in _iter_claim_entity_edges(conn, limit=limit):
            total_edges += 1
            if edge.claim_id != last_claim_id:
                processed_claims += 1
                last_claim_id = edge.claim_id
                if progress_every and processed_claims % progress_every == 0:
                    elapsed = time.perf_counter() - start
                    logger.info(
                        "progress: %d claims, %d edges, %.1fs",
                        processed_claims, total_edges, elapsed,
                    )
            buffer.append(edge)
            if dry_run:
                continue
            if len(buffer) >= BATCH:
                written += store.ingest_edges(buffer)
                buffer.clear()
        if buffer and not dry_run:
            written += store.ingest_edges(buffer)
            buffer.clear()
    finally:
        conn.close()
        if store is not None:
            store.close()

    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return {
        "claims_processed": processed_claims,
        "edges_considered": total_edges,
        "edges_written": written if not dry_run else 0,
        "dry_run": dry_run,
        "db_path": str(db_path),
        "graph_path": str(graph_path),
        "elapsed_ms": round(elapsed_ms, 1),
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="memorymaster.db",
                    help="SQLite DB to read (default: memorymaster.db)")
    ap.add_argument("--graph-path", default=str(_default_graph_path()),
                    help="Kuzu DB directory "
                         "(default: ~/.memorymaster/graph.kuzu)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Count edges without writing")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N claims")
    ap.add_argument("--progress-every", type=int, default=500,
                    help="Log every N claims (default: 500)")
    ap.add_argument("--allow-networkx", action="store_true",
                    help="Use in-memory networkx fallback when Kuzu is "
                         "unavailable. NOT persistent; useful for tests.")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = REPO / db_path
    graph_path = Path(os.path.expanduser(args.graph_path))

    try:
        summary = run_backfill(
            db_path=db_path,
            graph_path=graph_path,
            dry_run=args.dry_run,
            limit=args.limit,
            progress_every=args.progress_every,
            allow_networkx=args.allow_networkx,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except GraphStoreUnavailable:
        return 3

    print(
        f"[backfill] claims={summary['claims_processed']} "
        f"edges_considered={summary['edges_considered']} "
        f"edges_written={summary['edges_written']} "
        f"dry_run={summary['dry_run']} "
        f"elapsed_ms={summary['elapsed_ms']}"
    )
    print(f"[backfill] graph_path={summary['graph_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
