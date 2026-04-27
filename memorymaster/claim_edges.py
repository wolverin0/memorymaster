"""Structural claim_edges — call/reference edges between claims (v3.9.0 F8).

Inspired by gbrain v0.21.0 "Code Cathedral II" call-graph edges. Where their
edges connect code symbols, ours connect CLAIMS. Two semantic edge kinds:

* ``mentions`` — claim_a's text mentions a substring that uniquely identifies
  claim_b (its mm-<hex> human_id, or a phrase like "claim 12345").
* ``supersedes`` — already exists in the lifecycle as ``replaced_by_claim_id``;
  the edges table mirrors it for symmetric BFS walks.

This module ships the schema + populate/walk primitives. Wiring into the
recall hook's two-pass stream (F5) is a follow-up — the walker is exposed as
``walk_neighbors(claim_id, max_hops)`` so the recall hook can opt-in via env
flag in a future patch without re-touching this module.
"""
from __future__ import annotations

import re
import sqlite3
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "ensure_claim_edges_schema",
    "extract_edges_for_claim",
    "rebuild_edges",
    "walk_neighbors",
    "MENTION_KIND",
    "SUPERSEDES_KIND",
]


MENTION_KIND = "mentions"
SUPERSEDES_KIND = "supersedes"


_CLAIM_NUM_RE = re.compile(r"\bclaims?\s+(\d{1,6})\b", re.IGNORECASE)
_CLAIM_MM_RE = re.compile(r"\b(mm-[a-f0-9]{4,}(?:~[0-9]+)?)\b", re.IGNORECASE)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS claim_edges (
    src_claim_id INTEGER NOT NULL,
    dst_claim_id INTEGER NOT NULL,
    edge_kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (src_claim_id, dst_claim_id, edge_kind)
);

CREATE INDEX IF NOT EXISTS idx_claim_edges_src ON claim_edges(src_claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_edges_dst ON claim_edges(dst_claim_id);
"""


def ensure_claim_edges_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_SQL)
    conn.commit()


def extract_edges_for_claim(
    conn: sqlite3.Connection, src_claim_id: int, src_text: str
) -> list[tuple[int, int, str]]:
    """Return ``[(src, dst, kind)]`` for every reference inside ``src_text``.

    Resolves mm-<hex> tokens and ``claim NNNN`` numerics against the live
    claims table; dst-claims that don't exist or that point back at src are
    silently dropped.
    """
    if not src_text:
        return []
    edges: list[tuple[int, int, str]] = []
    seen_dst: set[int] = set()

    # Numeric refs: claim 12345
    for m in _CLAIM_NUM_RE.finditer(src_text):
        try:
            dst = int(m.group(1))
        except ValueError:
            continue
        if dst == src_claim_id or dst in seen_dst:
            continue
        row = conn.execute(
            "SELECT 1 FROM claims WHERE id = ? LIMIT 1", (dst,)
        ).fetchone()
        if row is None:
            continue
        seen_dst.add(dst)
        edges.append((src_claim_id, dst, MENTION_KIND))

    # Human-id refs: mm-1a2b
    for m in _CLAIM_MM_RE.finditer(src_text):
        human = m.group(1).lower()
        row = conn.execute(
            "SELECT id FROM claims WHERE LOWER(human_id) = ? LIMIT 1",
            (human,),
        ).fetchone()
        if row is None:
            continue
        dst = int(row[0])
        if dst == src_claim_id or dst in seen_dst:
            continue
        seen_dst.add(dst)
        edges.append((src_claim_id, dst, MENTION_KIND))

    return edges


def rebuild_edges(db_path: str | Path, *, batch_size: int = 500) -> dict[str, int]:
    """Walk the entire claims table and rebuild the claim_edges index.

    Returns counters: ``{"claims_scanned": N, "edges_written": M, "supersession_edges": K}``.

    Idempotent: ``INSERT OR IGNORE`` against the composite primary key.
    """
    counters = {"claims_scanned": 0, "edges_written": 0, "supersession_edges": 0}
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_claim_edges_schema(conn)
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            "SELECT id, text, replaced_by_claim_id FROM claims "
            "WHERE text IS NOT NULL AND TRIM(text) != ''"
        )
        rows = cursor.fetchall()
        for src_id, text, replaced_by in rows:
            counters["claims_scanned"] += 1
            edges = extract_edges_for_claim(conn, int(src_id), text or "")
            if replaced_by is not None and int(replaced_by) != int(src_id):
                row = conn.execute(
                    "SELECT 1 FROM claims WHERE id = ? LIMIT 1", (int(replaced_by),)
                ).fetchone()
                if row is not None:
                    edges.append((int(src_id), int(replaced_by), SUPERSEDES_KIND))
                    counters["supersession_edges"] += 1
            for src, dst, kind in edges:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO claim_edges "
                    "(src_claim_id, dst_claim_id, edge_kind, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (src, dst, kind, now),
                )
                if cur.rowcount:
                    counters["edges_written"] += 1
            if counters["claims_scanned"] % batch_size == 0:
                conn.commit()
        conn.commit()
    finally:
        conn.close()
    return counters


def walk_neighbors(
    db_path: str | Path,
    seed_claim_ids: list[int],
    *,
    max_hops: int = 2,
    direction: str = "both",
) -> dict[int, int]:
    """BFS from seeds; return ``{neighbor_claim_id: hop_distance}``.

    Args:
        seed_claim_ids: starting set.
        max_hops: BFS depth limit (1 = direct neighbors only).
        direction: ``"out"``, ``"in"``, or ``"both"``. ``"both"`` is the
            default since claim references are usually meaningful in either
            direction.

    Seeds are NOT included in the result. Empty result on any DB error.
    """
    if not seed_claim_ids or max_hops < 1:
        return {}
    distances: dict[int, int] = {}
    seen: set[int] = set(int(s) for s in seed_claim_ids)
    queue: deque[tuple[int, int]] = deque()
    for s in seed_claim_ids:
        queue.append((int(s), 0))
    conn = sqlite3.connect(str(db_path))
    try:
        while queue:
            cid, depth = queue.popleft()
            if depth >= max_hops:
                continue
            ids: set[int] = set()
            try:
                if direction in ("out", "both"):
                    cursor = conn.execute(
                        "SELECT dst_claim_id FROM claim_edges WHERE src_claim_id = ?",
                        (cid,),
                    )
                    ids.update(int(r[0]) for r in cursor.fetchall())
                if direction in ("in", "both"):
                    cursor = conn.execute(
                        "SELECT src_claim_id FROM claim_edges WHERE dst_claim_id = ?",
                        (cid,),
                    )
                    ids.update(int(r[0]) for r in cursor.fetchall())
            except sqlite3.OperationalError:
                # claim_edges table missing — just bail
                return {}
            for n in ids:
                if n in seen:
                    continue
                seen.add(n)
                distances[n] = depth + 1
                queue.append((n, depth + 1))
    finally:
        conn.close()
    return distances
