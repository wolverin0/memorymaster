"""0006_verbatim_session_content_index — composite dedup index (verbatim-perf).

``verbatim_store._store_verbatim_conn`` dedups every insert with::

    SELECT id FROM verbatim_memories WHERE session_id = ? AND content = ? LIMIT 1

On hot orchestrator sessions the content blob is up to ~262 KB. Without a
composite index on ``(session_id, content)`` SQLite falls back to the
single-column ``idx_verbatim_session`` and then byte-compares the ~262 KB
``content`` of every other row in that session — O(rows-in-session) full-blob
comparisons per insert. The same shape powers ``verbatim_cleanup.cleanup``'s
correlated ``EXISTS`` self-join, which without this index is effectively
quadratic over the whole table (823k rows observed).

A composite ``(session_id, content)`` index turns both into an index seek: the
dedup ``SELECT`` becomes a direct probe, and the cleanup ``EXISTS`` inner query
becomes an index lookup instead of a per-row scan. Audit reconciliation: the
index was *not* present anywhere (no migration 0005 shipped it; schema.sql does
not manage the verbatim tables at all — they are created by the installed Stop
hook), so this migration introduces it for the first time.

The ``verbatim_memories`` table is created out-of-band by the Stop hook, not by
the claims baseline schema, so it may legitimately be absent when this migration
runs on a claims-only DB. We therefore gate the index on the table's existence
(mirroring 0004's claims-trigger guard) and use ``IF NOT EXISTS`` so re-runs and
DBs that already grew the index by hand are no-ops. Postgres support is included
for SQLite<->Postgres parity even though verbatim is SQLite-only today.
"""
from __future__ import annotations

VERSION = 6
DESCRIPTION = "composite idx_verbatim_session_content(session_id, content) for sargable dedup/cleanup"

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_verbatim_session_content "
    "ON verbatim_memories(session_id, content)"
)


def apply_sqlite(conn) -> None:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='verbatim_memories'"
    ).fetchone()
    if has_table:
        conn.execute(_INDEX_DDL)
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def apply_postgres(conn) -> None:
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('verbatim_memories')")
    row = cur.fetchone()
    if row and row[0] is not None:
        cur.execute(_INDEX_DDL)
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()
