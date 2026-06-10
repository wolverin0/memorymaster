"""0002_miner_state — KV table for resumable miner watermarks (v3.21.0-R1b).

The verbatim rule-miner (:mod:`memorymaster.knowledge.rule_miner`) needs to remember
how far through ``verbatim_memories`` it has scanned so re-runs are
incremental. The watermark is a single integer, not a per-row fact, so it
lives in a small key-value table rather than a column on the 744k-row
verbatim table (a column would force a full backfill and contend with the
Stop hook's inserts under WAL).

A KV table also generalizes: future miners reuse it with different keys.
"""
from __future__ import annotations

VERSION = 2
DESCRIPTION = "miner_state KV table for resumable verbatim mining watermarks"

_DDL = """
CREATE TABLE IF NOT EXISTS miner_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
)
""".strip()


def apply_sqlite(conn) -> None:
    conn.execute(_DDL)
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def apply_postgres(conn) -> None:
    cur = conn.cursor()
    cur.execute(_DDL)
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()
