"""Verbatim archive cleanup (v3.23).

The verbatim_memories table accumulates two distinct kinds of bloat:

1. **Junk from the pre-#128 capture bug**: the Stop hook's store_transcript
   used to read top-level entry fields (the actual turns were nested under
   ``message``), so the only rows it stored were a handful of non-conversation
   metadata lines per transcript — most of them duplicated internal-LLM prompts
   like "Rewrite ONLY the compiled truth section…". Tens of thousands of rows
   carrying the same internal prompt text, with empty ``role``.

2. **Duplicate insertions**: even after the capture fix, re-running
   store_transcript on the same JSONL is dedup-checked per (session_id, content),
   but cross-session duplicates and pre-WAL-fix collisions persist.

This module reports + reclaims both. It DELETES rows directly (verbatim has no
foreign keys), and reflushes the FTS5 mirror so search stays consistent. Dry-run
by default; ``--apply`` to actually delete.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

# Known pre-#128 junk content prefixes (internal LLM prompts that the broken
# capture path mistakenly stored as if they were conversation turns).
_JUNK_PREFIXES: tuple[str, ...] = (
    "Rewrite ONLY the compiled truth",
    "You are a memory curator",
    "You are an expert at compiling",
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _verbatim_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='verbatim_memories'"
    ).fetchone()
    return row is not None


def _fts_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='verbatim_fts'"
    ).fetchone()
    return row is not None


def analyze(db_path: str) -> dict[str, Any]:
    """Report verbatim composition without touching anything.

    Returns counts: total rows, distinct contents, exact duplicates (per
    (session_id, content)), pre-#128 junk rows (matching known prefixes), and
    rows with empty role (legacy capture-bug signature).
    """
    if "://" in str(db_path):
        raise ValueError("verbatim cleanup is SQLite-only")
    conn = _connect(db_path)
    try:
        if not _verbatim_present(conn):
            return {"verbatim_present": False}
        total = conn.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()[0]
        distinct_content = conn.execute(
            "SELECT COUNT(DISTINCT content) FROM verbatim_memories"
        ).fetchone()[0]
        # Exact duplicates by (session_id, content): the "extra" copies beyond
        # the first per group are deletable.
        dup_extras = conn.execute(
            """SELECT COALESCE(SUM(c - 1), 0) FROM (
                   SELECT COUNT(*) AS c FROM verbatim_memories
                   GROUP BY session_id, content HAVING c > 1
               )"""
        ).fetchone()[0]
        empty_role = conn.execute(
            "SELECT COUNT(*) FROM verbatim_memories WHERE COALESCE(role, '') = ''"
        ).fetchone()[0]
        junk = 0
        for prefix in _JUNK_PREFIXES:
            junk += conn.execute(
                "SELECT COUNT(*) FROM verbatim_memories WHERE content LIKE ?",
                (prefix + "%",),
            ).fetchone()[0]
        return {
            "verbatim_present": True,
            "total": int(total),
            "distinct_content": int(distinct_content),
            "duplicate_extras": int(dup_extras),
            "empty_role_rows": int(empty_role),
            "junk_prefix_rows": int(junk),
            "junk_prefixes": list(_JUNK_PREFIXES),
        }
    finally:
        conn.close()


def cleanup(
    db_path: str,
    *,
    dedup: bool = True,
    purge_junk: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Reclaim space. Default is dry-run (counts only); ``dry_run=False`` to
    actually delete. ``purge_junk`` is opt-in because it targets a known-bad
    pattern set — review with ``analyze`` first.

    Returns counts of what was (or would be) removed, and the new total.
    """
    if "://" in str(db_path):
        raise ValueError("verbatim cleanup is SQLite-only")
    conn = _connect(db_path)
    try:
        if not _verbatim_present(conn):
            return {"verbatim_present": False}
        out: dict[str, Any] = {
            "dry_run": bool(dry_run),
            "dedup_deleted": 0,
            "junk_deleted": 0,
            "before_total": int(conn.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()[0]),
        }
        has_fts = _fts_present(conn)
        deleted_ids: list[int] = []

        if dedup:
            # Identify ids to drop: for each (session_id, content) group, keep
            # the smallest id (oldest), drop the rest.
            rows = conn.execute(
                """SELECT id FROM verbatim_memories WHERE id NOT IN (
                       SELECT MIN(id) FROM verbatim_memories GROUP BY session_id, content
                   )"""
            ).fetchall()
            ids = [int(r[0]) for r in rows]
            out["dedup_deleted"] = len(ids)
            deleted_ids.extend(ids)

        if purge_junk:
            for prefix in _JUNK_PREFIXES:
                rows = conn.execute(
                    "SELECT id FROM verbatim_memories WHERE content LIKE ?",
                    (prefix + "%",),
                ).fetchall()
                ids = [int(r[0]) for r in rows]
                out["junk_deleted"] += len(ids)
                deleted_ids.extend(ids)

        if not dry_run and deleted_ids:
            # Chunked DELETE to keep SQLite parameter binding bounded.
            BATCH = 500
            for i in range(0, len(deleted_ids), BATCH):
                chunk = deleted_ids[i : i + BATCH]
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(
                    f"DELETE FROM verbatim_memories WHERE id IN ({placeholders})", chunk
                )
                if has_fts:
                    conn.execute(
                        f"DELETE FROM verbatim_fts WHERE rowid IN ({placeholders})", chunk
                    )
            conn.commit()

        out["after_total"] = int(
            conn.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()[0]
        )
        return out
    finally:
        conn.close()
