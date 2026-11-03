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

import hashlib
import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from memorymaster.stores._storage_shared import connect_ro, open_conn

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_SESSIONS = 75_000

# Known pre-#128 junk content prefixes (internal LLM prompts that the broken
# capture path mistakenly stored as if they were conversation turns).
_JUNK_PREFIXES: tuple[str, ...] = (
    "Rewrite ONLY the compiled truth",
    "You are a memory curator",
    "You are an expert at compiling",
)


def _connect(db_path: str) -> sqlite3.Connection:
    return open_conn(db_path)


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


def _junk_prefix_count(conn: sqlite3.Connection) -> int:
    """Count junk-prefix rows in a SINGLE table pass.

    The previous implementation ran one ``content LIKE ?`` query per prefix —
    three sequential full-table scans over a multi-GB table for a read-only
    report. The prefixes are disjoint (a row matches at most one), so OR-ing
    them into one predicate gives the identical total in one pass. Still fully
    parameterized: one ``?`` per prefix, no value concatenation into the SQL.
    """
    if not _JUNK_PREFIXES:
        return 0
    where = " OR ".join("content LIKE ?" for _ in _JUNK_PREFIXES)
    params = tuple(p + "%" for p in _JUNK_PREFIXES)
    row = conn.execute(
        f"SELECT COUNT(*) FROM verbatim_memories WHERE {where}", params
    ).fetchone()
    return int(row[0])


def analyze(db_path: str, *, deep: bool = True) -> dict[str, Any]:
    """Report verbatim composition without touching anything.

    Counts: total rows, distinct contents, exact duplicates (per
    (session_id, content)), pre-#128 junk rows, and empty-role rows.

    ``deep`` (default True = legacy behaviour) gates the two expensive
    whole-table aggregations (``COUNT(DISTINCT content)`` and the
    ``GROUP BY`` duplicate tally) that turn a quick read into a multi-minute
    scan on a multi-GB table. With ``deep=False`` those two come back as
    ``None`` and only the cheap counts are computed.
    """
    if "://" in str(db_path):
        raise ValueError("verbatim cleanup is SQLite-only")
    conn = _connect(db_path)
    try:
        if not _verbatim_present(conn):
            return {"verbatim_present": False}
        total = conn.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()[0]
        distinct_content: int | None = None
        dup_extras: int | None = None
        if deep:
            distinct_content = int(conn.execute(
                "SELECT COUNT(DISTINCT content) FROM verbatim_memories"
            ).fetchone()[0])
            # Exact duplicates by (session_id, content): the "extra" copies
            # beyond the first per group are deletable.
            dup_extras = int(conn.execute(
                """SELECT COALESCE(SUM(c - 1), 0) FROM (
                       SELECT COUNT(*) AS c FROM verbatim_memories
                       GROUP BY session_id, content HAVING c > 1
                   )"""
            ).fetchone()[0])
        empty_role = conn.execute(
            "SELECT COUNT(*) FROM verbatim_memories WHERE COALESCE(role, '') = ''"
        ).fetchone()[0]
        junk = _junk_prefix_count(conn)
        return {
            "verbatim_present": True,
            "deep": bool(deep),
            "total": int(total),
            "distinct_content": distinct_content,
            "duplicate_extras": dup_extras,
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
            # the smallest id (oldest), drop the rest. NOT EXISTS against a
            # correlated "is there an older twin?" probe avoids the O(n^2)
            # full-table anti-join that `id NOT IN (SELECT MIN(id) ...)` forces
            # SQLite into on the cold CLI path. Results are identical: a row is
            # dropped iff another row in the same (session_id, content) group has
            # a strictly smaller id.
            rows = conn.execute(
                """SELECT id FROM verbatim_memories AS v
                   WHERE EXISTS (
                       SELECT 1 FROM verbatim_memories AS older
                       WHERE older.session_id IS v.session_id
                         AND older.content = v.content
                         AND older.id < v.id
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


def _read_retention_rows(db_path: str, scan_limit: int) -> tuple[bool, list[dict[str, Any]], bool]:
    conn = connect_ro(db_path)
    try:
        if not _verbatim_present(conn):
            return False, [], False
        rows = conn.execute(
            """SELECT id, session_id, length(CAST(content AS BLOB)) AS byte_count,
                      COALESCE(NULLIF(timestamp, ''), created_at) AS retained_at
               FROM verbatim_memories ORDER BY retained_at ASC, id ASC LIMIT ?""",
            (scan_limit + 1,),
        ).fetchall()
        return True, [dict(row) for row in rows[:scan_limit]], len(rows) > scan_limit
    finally:
        conn.close()


def _retained_at(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def plan_retention(
    db_path: str,
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    scan_limit: int = 100_000,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Plan finite verbatim retention without changing persistent data."""
    if min(max_age_days, max_bytes, max_sessions) < 0 or scan_limit <= 0:
        raise ValueError("retention limits must be non-negative")
    verbatim_present, rows, truncated = _read_retention_rows(db_path, scan_limit)
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=max_age_days)
    session_last: dict[str, datetime] = {}
    for row in rows:
        key = str(row["session_id"])
        session_last[key] = max(session_last.get(key, datetime.min.replace(tzinfo=timezone.utc)), _retained_at(row["retained_at"]))
    kept_sessions = {key for key, _ in sorted(session_last.items(), key=lambda item: item[1], reverse=True)[:max_sessions]}
    candidates = {
        int(row["id"])
        for row in rows
        if _retained_at(row["retained_at"]) < cutoff or str(row["session_id"]) not in kept_sessions
    }
    retained = [row for row in rows if int(row["id"]) not in candidates]
    retained_bytes = sum(int(row["byte_count"] or 0) for row in retained)
    for row in retained:
        if retained_bytes <= max_bytes:
            break
        candidates.add(int(row["id"]))
        retained_bytes -= int(row["byte_count"] or 0)
    return {
        "dry_run": True,
        "verbatim_present": verbatim_present,
        "truncated": truncated,
        "scan_limit": scan_limit,
        "limits": {"max_age_days": max_age_days, "max_bytes": max_bytes, "max_sessions": max_sessions},
        "total_rows": None if truncated else len(rows),
        "candidate_rows": len(candidates),
        "candidate_ids_sample": sorted(candidates)[:100],
        "retained_rows": len(rows) - len(candidates),
        "retained_bytes": retained_bytes,
        # `truncated` is reported but is not a bound: on a table larger than scan_limit it is always
        # true, which made this gate fail forever and silently stopped verbatim storage (T-2116).
        "within_bounds": not candidates,
    }


# --- T-2116: archive (move, never lose) rows older than N days into a separate DB ----------------

DEFAULT_ARCHIVE_DAYS = 90
ARCHIVE_GRACE_DAYS = 14  # retention counts as overdue only after two missed weekly runs
DEFAULT_ARCHIVE_BATCH = 2000
ARCHIVE_DB_NAME = "memorymaster-verbatim-archive.db"
_IN_CHUNK = 500  # bound SQLite parameter binding on old builds
_COLUMNS = ("id", "session_id", "role", "content", "scope", "timestamp", "source_agent",
            "embedding_synced", "created_at")
_COL_LIST = ", ".join(_COLUMNS)
_RETAINED_AT = "COALESCE(NULLIF(timestamp, ''), created_at)"
# shortcut: string-compares the first 19 chars (UTC wall clock, offset and 'T'/space ignored), so a
# row within hours of the cutoff may land on either side; irrelevant for a 90-day window.
_IS_DATED = f"substr({_RETAINED_AT}, 1, 10) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
_IS_OLD = f"substr({_RETAINED_AT}, 1, 19) < ?"


class ArchiveVerifyError(RuntimeError):
    """The copy could not be proven identical; nothing was deleted from the source."""


def _row_hash(row: Any) -> str:
    # embedding_synced is deliberately outside the hash: the Qdrant sync flips it on live rows and
    # that must not read as the row having changed.
    payload = "\x1f".join(str(row[name]) for name in _COLUMNS if name != "embedding_synced")
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


def _chunks(ids: list[int]) -> Iterator[list[int]]:
    for i in range(0, len(ids), _IN_CHUNK):
        yield ids[i : i + _IN_CHUNK]


def _rows_by_id(conn: sqlite3.Connection, ids: list[int]) -> dict[int, Any]:
    found: dict[int, Any] = {}
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(f"SELECT {_COL_LIST} FROM verbatim_memories WHERE id IN ({marks})", chunk):
            found[int(row["id"])] = row
    return found


def retention_due(
    db_path: str, *, days: int = DEFAULT_ARCHIVE_DAYS, grace_days: int = ARCHIVE_GRACE_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """O(1) staleness check for the Stop hook: is the oldest row older than ``days + grace_days``?

    Reads one row (lowest id; ids ascend with time). ``plan_retention`` sorts a computed column
    over the whole table and cannot run per Stop on a million-row table.
    """
    out: dict[str, Any] = {"due": False, "oldest_at": None, "oldest_age_days": None}
    if "://" in str(db_path) or not Path(str(db_path)).exists():
        return out
    conn = connect_ro(db_path)
    try:
        if not _verbatim_present(conn):
            return out
        # Same predicate as archive_old: an undated row is never archived, so it must not be able to
        # become the permanent head row and blind (or poison) this check.
        row = conn.execute(
            f"SELECT {_RETAINED_AT} AS at FROM verbatim_memories WHERE {_IS_DATED} ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row["at"]:
        return out
    try:
        oldest = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - oldest).days
    except ValueError:  # dated prefix but a malformed rest, e.g. 2026-13-45
        return out
    out.update(oldest_at=str(row["at"]), oldest_age_days=age, due=age > days + grace_days)
    return out


def _move_batch(src: sqlite3.Connection, dst: sqlite3.Connection, rows: list[Any],
                src_fts: bool, dst_fts: bool) -> None:
    """Copy one batch into the archive, prove it, commit it, then delete it from the source.

    The archive commit comes first and the source delete second, each in its own transaction (WAL
    gives no atomicity across two files). A crash in between leaves the rows in both DBs, which the
    idempotent rerun resolves; the opposite order could lose rows.
    """
    wanted = {int(r["id"]): _row_hash(r) for r in rows}
    ids = list(wanted)
    dst.execute("BEGIN IMMEDIATE")
    try:
        have = {i: _row_hash(r) for i, r in _rows_by_id(dst, ids).items()}
        clash = [i for i, h in have.items() if h != wanted[i]]
        if clash:
            raise ArchiveVerifyError(f"archive already holds different content for ids {clash[:5]}")
        fresh = [r for r in rows if int(r["id"]) not in have]
        dst.executemany(
            f"INSERT INTO verbatim_memories ({_COL_LIST}) VALUES ({','.join('?' for _ in _COLUMNS)})",
            [tuple(r[name] for name in _COLUMNS) for r in fresh],
        )
        if dst_fts:
            dst.executemany("INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)",
                            [(int(r["id"]), r["content"]) for r in fresh])
        copied = {i: _row_hash(r) for i, r in _rows_by_id(dst, ids).items()}
        if copied != wanted:
            raise ArchiveVerifyError("archive copy does not match the source batch; rolled back")
        dst.commit()
    except BaseException:
        dst.rollback()
        raise
    src.execute("BEGIN IMMEDIATE")
    try:
        current = {i: _row_hash(r) for i, r in _rows_by_id(src, ids).items()}
        if current != wanted:
            raise ArchiveVerifyError("source rows changed during the move; nothing deleted")
        for chunk in _chunks(ids):
            marks = ",".join("?" for _ in chunk)
            if src_fts:
                # External-content FTS5: drop the entry while the content row still exists. Deleting
                # a rowid that was never indexed raises "malformed", so only delete the indexed ones
                # (docsize holds one row per indexed document).
                indexed = [int(r[0]) for r in src.execute(
                    f"SELECT id FROM verbatim_fts_docsize WHERE id IN ({marks})", chunk)]
                for part in _chunks(indexed):
                    src.execute(
                        f"DELETE FROM verbatim_fts WHERE rowid IN ({','.join('?' for _ in part)})", part)
            src.execute(f"DELETE FROM verbatim_memories WHERE id IN ({marks})", chunk)
        src.commit()
    except BaseException:
        src.rollback()
        raise


def archive_old(
    db_path: str,
    *,
    days: int = DEFAULT_ARCHIVE_DAYS,
    batch_size: int = DEFAULT_ARCHIVE_BATCH,
    archive_path: str | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Move verbatim rows older than ``days`` (and their FTS entries) into a separate archive DB.

    Rows keep their ids and the same schema. Each batch is copied, hash-verified and committed to
    the archive before it is deleted from the source, so an interrupted or repeated run loses
    nothing and re-copies nothing. Rows with an unparseable timestamp are never moved. Default is
    a dry run that opens the source read-only and creates no archive file.
    """
    if "://" in str(db_path):
        raise ValueError("verbatim archive is SQLite-only")
    if days < 1 or batch_size < 1:
        raise ValueError("days and batch_size must be at least 1")
    target = Path(archive_path) if archive_path else Path(db_path).resolve().parent / ARCHIVE_DB_NAME
    if target.resolve() == Path(db_path).resolve():
        raise ValueError("archive path must differ from the source database")
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    out: dict[str, Any] = {"dry_run": bool(dry_run), "days": days, "cutoff": cutoff,
                           "archive_path": str(target)}
    if dry_run:
        conn = connect_ro(db_path)
        try:
            if not _verbatim_present(conn):
                return {"verbatim_present": False, **out}
            row = conn.execute(
                f"""SELECT COUNT(CASE WHEN {_IS_DATED} AND {_IS_OLD} THEN 1 END) AS n,
                           COALESCE(SUM(CASE WHEN {_IS_DATED} AND {_IS_OLD}
                                        THEN length(CAST(content AS BLOB)) END), 0) AS b,
                           COUNT(CASE WHEN NOT ({_IS_DATED}) THEN 1 END) AS bad
                    FROM verbatim_memories""",
                (cutoff, cutoff),
            ).fetchone()
        finally:
            conn.close()
        return {"verbatim_present": True, **out, "candidate_rows": int(row["n"]),
                "candidate_bytes": int(row["b"]), "skipped_unparseable": int(row["bad"])}

    from memorymaster.recall.verbatim_store import ensure_verbatim_schema

    src = _connect(db_path)
    try:
        if not _verbatim_present(src):
            return {"verbatim_present": False, **out}
        ensure_verbatim_schema(str(target))
        dst = _connect(str(target))
        try:
            src_fts, dst_fts = _fts_present(src), _fts_present(dst)
            moved = batches = 0
            cursor = 0
            while True:
                rows = src.execute(
                    f"SELECT {_COL_LIST} FROM verbatim_memories "
                    f"WHERE id > ? AND {_IS_DATED} AND {_IS_OLD} ORDER BY id LIMIT ?",
                    (cursor, cutoff, batch_size),
                ).fetchall()
                if not rows:
                    break
                _move_batch(src, dst, rows, src_fts, dst_fts)
                cursor = int(rows[-1]["id"])
                moved += len(rows)
                batches += 1
            bad = src.execute(
                f"SELECT COUNT(*) FROM verbatim_memories WHERE NOT ({_IS_DATED})"
            ).fetchone()[0]
            out.update(
                verbatim_present=True, rows_moved=moved, batches=batches, skipped_unparseable=int(bad),
                source_rows=int(src.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()[0]),
                archive_rows=int(dst.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()[0]),
            )
            return out
        finally:
            dst.close()
    finally:
        src.close()
