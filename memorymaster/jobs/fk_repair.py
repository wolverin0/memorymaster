"""Orphan-FK repair job (P1 WAL-discipline spec §2.6).

Repairs the orphan foreign-key rows left behind by the 2026-06-05 index
corruption recovery (spec F10: 401 verified orphans — events→claims 226,
citations→claims 159, claim_links→claims 6, claim_embeddings→claims 6,
claims self-FK 4). Dry-run by default; ``apply=True`` performs the repair
in ONE transaction:

1. ``PRAGMA foreign_key_check`` → group by (table, parent).
2. Export every orphan row verbatim to a quarantine JSONL
   (``~/.memorymaster/quarantine/fk-repair-<ts>.jsonl`` — outside the
   OneDrive-synced tree; audit trail, restorable). The file is fsynced
   BEFORE any row is mutated.
3. Dispose: ``events``/``citations``/``claim_links``/``claim_embeddings``
   orphans → DELETE (children of lost claims, meaningless without parents);
   ``claims`` self-FK orphans (dangling ``supersedes_claim_id``/
   ``replaced_by_claim_id``) → NULL the dangling pointer, keep the claim,
   emit a ``fk_repair``-marked event per touched claim. No status edits —
   only the dangling FK column is nulled (claims-lifecycle rules).
4. Re-run ``foreign_key_check`` inside the transaction; any handled-table
   orphan still present → ROLLBACK. Print before/after.

Idempotent: a second run finds zero orphans and is a no-op.

Reality notes (verified against the working tree, 2026-06-10):

- The ``events`` table is append-only (``trg_events_append_only_*``
  triggers) AND hash-chained (``event_hash``/``prev_event_hash``). The
  triggers are dropped and recreated inside the repair transaction. Deleting
  an orphan event leaves a ``broken_prev_link`` gap in the hash chain that
  ``reconcile_integrity`` will report — accepted by the spec ("children of
  lost claims, meaningless without parents"); the quarantine line preserves
  each deleted row's hashes so every gap is auditable.
- Per-claim repair events are emitted via ``store.record_event`` AFTER the
  repair transaction commits (record_event owns its own connection and the
  hash-chain computation; hand-inserting inside our transaction would fork
  that logic). A rollback therefore emits no events. ``fk_repair`` is not in
  ``models.EVENT_TYPES``, so the marker rides ``event_type='system'`` +
  ``details='fk_repair'`` — same convention as jobs/integrity.py markers.
- Orphans in tables outside the five observed shapes (e.g. action_proposals,
  whose FK is ON DELETE SET NULL) are reported as ``unhandled`` and never
  touched — default-deny beats guessing a disposal rule.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
from base64 import b64encode
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from memorymaster._storage_shared import (
    SQLITE_EVENTS_APPEND_ONLY_TRIGGERS,
    connect_ro,
    open_conn,
    utc_now,
)

logger = logging.getLogger(__name__)

MARKER_FK_REPAIR = "fk_repair"

# Disposal map (spec §2.6.3): children of lost claims are deleted; claims
# self-FK orphans keep the row and only NULL the dangling pointer.
DELETE_TABLES = ("events", "citations", "claim_links", "claim_embeddings")
SELF_FK_TABLE = "claims"

_EVENTS_APPEND_ONLY_TRIGGER_SQL = (
    """
    CREATE TRIGGER IF NOT EXISTS trg_events_append_only_update
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events table is append-only; UPDATE is not allowed');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_events_append_only_delete
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events table is append-only; DELETE is not allowed');
    END
    """,
)


def default_quarantine_dir() -> Path:
    """Quarantine dir — under the user home, outside the OneDrive-synced tree."""
    env = os.environ.get("MEMORYMASTER_QUARANTINE_DIR", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".memorymaster" / "quarantine"


def _scan(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """``PRAGMA foreign_key_check`` rows as dicts (table, rowid, parent, fkid)."""
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    return [
        {"table": str(r[0]), "rowid": r[1], "parent": str(r[2]), "fkid": r[3]}
        for r in rows
    ]


def _groups(orphans: list[dict[str, object]]) -> dict[str, int]:
    """Spec §2.6.1 grouping: ``{"<table>-><parent>": count}``."""
    counter = Counter(f"{o['table']}->{o['parent']}" for o in orphans)
    return dict(counter)


def _self_fk_columns(conn: sqlite3.Connection) -> dict[int, str]:
    """Map ``foreign_key_check`` fkid → claims self-FK column name."""
    return {
        int(r["id"]): str(r["from"])
        for r in conn.execute(f"PRAGMA foreign_key_list({SELF_FK_TABLE})").fetchall()
        if str(r["table"]) == SELF_FK_TABLE
    }


def _json_safe(value: object) -> object:
    if isinstance(value, bytes):
        return {"__bytes_b64__": b64encode(value).decode("ascii")}
    return value


def _row_verbatim(conn: sqlite3.Connection, table: str, rowid: object) -> dict[str, object]:
    """Full row content by rowid — the restorable audit record."""
    row = conn.execute(f'SELECT * FROM "{table}" WHERE rowid = ?', (rowid,)).fetchone()
    if row is None:
        return {}
    return {key: _json_safe(row[key]) for key in row.keys()}


def _write_quarantine(
    path: Path,
    conn: sqlite3.Connection,
    orphans: list[dict[str, object]],
) -> int:
    """Export orphan rows verbatim (one JSONL line per row, violations merged).

    fsynced before returning so the audit trail is durable BEFORE any
    mutation can commit.
    """
    by_row: dict[tuple[str, object], dict[str, object]] = {}
    for o in orphans:
        key = (str(o["table"]), o["rowid"])
        entry = by_row.setdefault(
            key,
            {
                "exported_at": utc_now(),
                "table": o["table"],
                "rowid": o["rowid"],
                "violations": [],
                "row": _row_verbatim(conn, str(o["table"]), o["rowid"]),
            },
        )
        entry["violations"].append({"parent": o["parent"], "fkid": o["fkid"]})  # type: ignore[union-attr]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for entry in by_row.values():
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return len(by_row)


def _plan(orphans: list[dict[str, object]]) -> dict[str, object]:
    """Disposal plan: per-table delete counts, pointer-null count, unhandled."""
    deletes: Counter[str] = Counter()
    nulls = 0
    unhandled: Counter[str] = Counter()
    for o in orphans:
        table = str(o["table"])
        if table in DELETE_TABLES:
            deletes[table] += 1
        elif table == SELF_FK_TABLE:
            nulls += 1
        else:
            unhandled[table] += 1
    return {"delete": dict(deletes), "null_pointer": nulls, "unhandled": dict(unhandled)}


def _apply_repairs(
    conn: sqlite3.Connection,
    orphans: list[dict[str, object]],
) -> tuple[dict[str, int], list[dict[str, object]], int]:
    """Execute disposals inside the caller's transaction.

    Returns (deleted-per-table, nulled-pointer records, unhandled count).
    """
    deleted: Counter[str] = Counter()
    nulled: list[dict[str, object]] = []
    unhandled = 0
    fk_columns = _self_fk_columns(conn)

    delete_rowids: dict[str, set[object]] = {}
    for o in orphans:
        table = str(o["table"])
        if table in DELETE_TABLES:
            delete_rowids.setdefault(table, set()).add(o["rowid"])
        elif table == SELF_FK_TABLE:
            column = fk_columns.get(int(o["fkid"])) if o["fkid"] is not None else None
            candidates = [column] if column else list(fk_columns.values())
            for col in candidates:
                cur = conn.execute(
                    f"""
                    UPDATE claims SET {col} = NULL
                    WHERE rowid = ? AND {col} IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM claims p WHERE p.id = claims.{col})
                    """,
                    (o["rowid"],),
                )
                if cur.rowcount:
                    claim_id = conn.execute(
                        "SELECT id FROM claims WHERE rowid = ?", (o["rowid"],)
                    ).fetchone()[0]
                    nulled.append({"claim_id": int(claim_id), "column": col})
        else:
            unhandled += 1

    if "events" in delete_rowids:
        # events is append-only by trigger — lift the guard only inside this
        # transaction, delete the orphans, restore the guard.
        for trigger in SQLITE_EVENTS_APPEND_ONLY_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table, rowids in delete_rowids.items():
        for rowid in rowids:
            cur = conn.execute(f'DELETE FROM "{table}" WHERE rowid = ?', (rowid,))
            deleted[table] += cur.rowcount
    if "events" in delete_rowids:
        for trigger_sql in _EVENTS_APPEND_ONLY_TRIGGER_SQL:
            conn.execute(trigger_sql)

    return dict(deleted), nulled, unhandled


def run(
    store,
    *,
    db_path: str | Path | None = None,
    apply: bool = False,
    quarantine_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Scan (and optionally repair) orphan FK rows. Dry-run unless ``apply``."""
    db_path = db_path or getattr(store, "db_path", None)
    if not db_path or not Path(db_path).exists():
        return {"skipped": "no_sqlite_db"}

    if not apply:
        with closing(connect_ro(db_path)) as conn:
            orphans = _scan(conn)
        return {
            "mode": "dry-run",
            "before": len(orphans),
            "groups": _groups(orphans),
            "planned": _plan(orphans),
        }

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    qdir = Path(quarantine_dir) if quarantine_dir else default_quarantine_dir()
    qfile = qdir / f"fk-repair-{stamp}.jsonl"

    conn = open_conn(db_path)
    conn.isolation_level = None  # explicit transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")
        orphans = _scan(conn)
        if not orphans:
            conn.execute("ROLLBACK")
            return {"mode": "apply", "before": 0, "after": 0, "noop": True}

        quarantined = _write_quarantine(qfile, conn, orphans)
        deleted, nulled, unhandled = _apply_repairs(conn, orphans)
        remaining = _scan(conn)
        handled_remaining = [
            o for o in remaining
            if str(o["table"]) in DELETE_TABLES or str(o["table"]) == SELF_FK_TABLE
        ]
        if handled_remaining:
            conn.execute("ROLLBACK")
            logger.error(
                "fk_repair: %s handled orphans remain after repair — rolled back",
                len(handled_remaining),
            )
            return {
                "mode": "apply",
                "error": "handled orphans remain after repair; rolled back",
                "before": len(orphans),
                "remaining": _groups(remaining),
                "quarantine": str(qfile),
            }
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    # Post-commit: one fk_repair-marked event per touched claim (audit trail
    # for the kept-but-nulled claims; record_event owns the hash chain).
    for record in nulled:
        with contextlib.suppress(Exception):
            store.record_event(
                claim_id=int(record["claim_id"]),
                event_type="system",
                details=MARKER_FK_REPAIR,
                payload={"nulled_column": record["column"]},
            )

    result: dict[str, object] = {
        "mode": "apply",
        "before": len(orphans),
        "after": len(remaining),
        "groups": _groups(orphans),
        "deleted": deleted,
        "nulled": nulled,
        "unhandled": unhandled,
        "quarantined_rows": quarantined,
        "quarantine": str(qfile),
        "ok": not remaining,
    }
    logger.info(
        "fk_repair: before=%s after=%s deleted=%s nulled=%s quarantine=%s",
        len(orphans), len(remaining), deleted, len(nulled), qfile,
    )
    return result
