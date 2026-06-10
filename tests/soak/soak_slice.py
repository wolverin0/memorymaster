"""Fixture-slice builder for the chaos soak (spec §4; see chaos_soak.py).

Extracted from chaos_soak.py to keep that file under the 800-LOC boundary.
Invoked as ``python tests/soak/chaos_soak.py --build-slice`` (and from
``scripts/run_chaos_soak.ps1``); the source DB is opened strictly read-only.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def build_slice(
    source: Path,
    dest: Path,
    *,
    max_claims: int = 20000,
    max_verbatim: int = 50000,
) -> dict[str, int]:
    """Build a schema-identical, FK-consistent fixture slice from a live DB.

    The source is opened strictly read-only (mode=ro URI) — the live file is
    NEVER written. Spec §4 asks for a ~200 MB slice; a plain ``VACUUM INTO``
    of a 3.47 GB DB reproduces all 3.47 GB, so the builder instead seeds a
    fresh current-schema DB (init_db) and copies a bounded, FK-closed subset:
    the newest ``max_claims`` claims with their citations/events/links/
    embeddings, plus the newest ``max_verbatim`` verbatim turns (the table
    that actually corrupted). Dangling self-FK pointers are nulled the same
    way fk_repair does. Ends with a hard gate: foreign_key_check == 0 rows
    and quick_check == ok, so a bad fixture can never masquerade as a soak
    failure.
    """
    from memorymaster.storage import SQLiteStore
    from memorymaster.verbatim_store import ensure_verbatim_schema

    dest.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        leftover = Path(str(dest) + suffix)
        if leftover.exists():
            leftover.unlink()
    SQLiteStore(dest).init_db()
    ensure_verbatim_schema(str(dest))

    conn = sqlite3.connect(f"file:{dest.as_posix()}?mode=rw", uri=True)
    conn.row_factory = sqlite3.Row
    stats: dict[str, int] = {}
    try:
        conn.execute("ATTACH DATABASE ? AS src", (f"file:{source.as_posix()}?mode=ro",))

        def common_cols(table: str) -> list[str]:
            src_cols = {r[1] for r in conn.execute(f"PRAGMA src.table_info({table})")}
            dst_cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")]
            return [c for c in dst_cols if c in src_cols]

        def copy(table: str, tail: str = "", params: tuple = ()) -> int:
            cols = common_cols(table)
            if not cols:
                return 0
            collist = ", ".join(cols)
            sql = f"INSERT OR IGNORE INTO main.{table} ({collist}) SELECT {collist} FROM src.{table} {tail}"
            try:
                conn.execute(sql, params)
            except sqlite3.DatabaseError:
                # e.g. the confirmed-tuple guard trigger ABORTs the bulk
                # statement on a grandfathered duplicate: fall back row-wise
                # and skip only the offenders.
                placeholders = ", ".join("?" for _ in cols)
                for row in conn.execute(f"SELECT {collist} FROM src.{table} {tail}", params).fetchall():
                    try:
                        conn.execute(
                            f"INSERT OR IGNORE INTO main.{table} ({collist}) VALUES ({placeholders})",
                            tuple(row),
                        )
                    except sqlite3.DatabaseError:
                        continue
            return conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]

        stats["claims"] = copy("claims", f"ORDER BY id DESC LIMIT {int(max_claims)}")
        in_set = "(SELECT id FROM main.claims)"
        stats["citations"] = copy("citations", f"WHERE claim_id IN {in_set}")
        stats["events"] = copy("events", f"WHERE claim_id IN {in_set}")
        stats["claim_embeddings"] = copy("claim_embeddings", f"WHERE claim_id IN {in_set}")
        stats["claim_links"] = copy(
            "claim_links", f"WHERE source_id IN {in_set} AND target_id IN {in_set}"
        )
        for col in ("supersedes_claim_id", "replaced_by_claim_id"):
            conn.execute(
                f"UPDATE main.claims SET {col} = NULL"
                f" WHERE {col} IS NOT NULL AND {col} NOT IN {in_set}"
            )
        stats["verbatim_memories"] = copy(
            "verbatim_memories", f"ORDER BY id DESC LIMIT {int(max_verbatim)}"
        )
        conn.execute(
            "INSERT INTO verbatim_fts(rowid, content)"
            " SELECT id, content FROM main.verbatim_memories"
        )
        conn.commit()
        conn.execute("DETACH DATABASE src")

        # FK-close the fixture: drop any orphan child rows the bounded copy
        # produced in tables not handled above (events is append-only via
        # trigger, so lift the guard for fixture surgery only).
        for _ in range(10):
            orphan_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            if not orphan_rows:
                break
            for row in orphan_rows:
                table, rowid = row[0], row[1]
                if rowid is None:
                    continue
                if table == "events":
                    conn.execute("DROP TRIGGER IF EXISTS trg_events_append_only_delete")
                conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
            conn.commit()
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS trg_events_append_only_delete
            BEFORE DELETE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events table is append-only; DELETE is not allowed');
            END;
            """
        )
        conn.commit()

        remaining = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        if remaining:
            raise RuntimeError(f"slice still has {remaining} orphan FK rows")
        qc = [str(r[0]) for r in conn.execute("PRAGMA quick_check").fetchall()]
        if qc != ["ok"]:
            raise RuntimeError(f"slice quick_check failed: {qc}")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return stats
