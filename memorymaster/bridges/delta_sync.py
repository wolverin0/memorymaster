"""Delta export for incremental memory sync.

Pre-existing sync (``scripts/openclaw-sync.sh``) copied the WHOLE database
file across the network twice per cycle — fine when the DB was small,
wasteful at 2.5 GB. The ``merge-db`` step was always append-only (it dedups
on ``idempotency_key`` + text-hash), but the *transport* moved everything.

``export_delta`` closes that gap: it writes a small SQLite file containing
ONLY the claims (and their citations) changed since a watermark timestamp.
That small file is a valid merge source — ``merge-db --source delta.db``
consumes it unchanged, because the merge engine only reads ``claims`` and
``citations`` and ignores everything else.

Sync loop with deltas:

    1. each side: ``export-delta --since <last_sync> --output delta.db``
    2. ship the small delta.db over the network (KB, not GB)
    3. each side: ``merge-db --source <other-side-delta.db>``
    4. record the new watermark (``max_updated_at`` from the export result)

The whole-DB file never crosses the network — which also removes the
SQLite-over-network-mount corruption risk entirely.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from memorymaster.stores._storage_shared import connect_ro, open_conn
from memorymaster.stores.store_factory import is_postgres_dsn

logger = logging.getLogger(__name__)


# Tables a merge source must carry. The merge engine (db_merge.py) reads
# `claims` (SELECT *) and `citations` (source/locator/excerpt/created_at).
# Nothing else is needed — FTS5, events, entity tables are all rebuilt or
# ignored on the merge side.
_DELTA_TABLES = ("claims", "citations")


def _copy_table_ddl(src: sqlite3.Connection, out: sqlite3.Connection, table: str) -> None:
    """Copy a table's CREATE statement verbatim from src into out."""
    row = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None or not row[0]:
        raise ValueError(f"source DB has no '{table}' table — not a memorymaster DB?")
    out.execute(row[0])


def export_delta(
    source_db: str | Path,
    since: str,
    output_path: str | Path,
) -> dict[str, object]:
    """Write claims changed since ``since`` into a small SQLite delta file.

    Args:
        source_db: path to the full memorymaster DB to export from.
        since: ISO-8601 timestamp watermark. Claims with ``updated_at`` strictly
            greater than this are exported. Pass an empty string or a very
            early date to export everything (full bootstrap).
        output_path: where to write the delta SQLite file. Overwritten if it
            exists.

    Returns:
        dict with ``exported`` (claim count), ``citations`` (citation count),
        ``since`` (echoed watermark), and ``max_updated_at`` (the newest
        ``updated_at`` seen — use this as the next watermark; None when the
        delta is empty).

    Raises:
        FileNotFoundError: source DB missing.
        ValueError: source DB lacks the expected tables.
    """
    if is_postgres_dsn(str(source_db)) or is_postgres_dsn(str(output_path)):
        raise ValueError(
            "export-delta supports SQLite paths only; raw Postgres team deltas are disabled."
        )

    source_db = str(source_db)
    output_path = Path(output_path)
    if not Path(source_db).exists():
        raise FileNotFoundError(f"Source DB not found: {source_db}")

    # Fresh output file every time — a stale delta would merge old rows again
    # (harmless thanks to idempotent merge, but wasteful).
    if output_path.exists():
        output_path.unlink()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Source is read-only (connect_ro takes no lock on the live DB); the
    # fresh delta file gets the uniform writer envelope.
    src = connect_ro(source_db)
    out = open_conn(output_path)
    # The delta is a TRANSPORT file, not a live DB. The claims DDL is copied
    # verbatim (incl. supersedes/replaced_by FKs to claims.id), and a claim in
    # the window may legitimately reference a claim OUTSIDE it — with FK
    # enforcement on, one such row kills the whole export (this silently broke
    # the Windows->Hermes sync for 3 weeks: nightly 'FOREIGN KEY constraint
    # failed' since 2026-06-10). Integrity is re-established by the idempotent
    # merge into the target DB, which already holds (or dedups) the parents.
    out.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in _DELTA_TABLES:
            _copy_table_ddl(src, out, table)

        # `since` empty => full export. SQLite string comparison on ISO-8601
        # timestamps is chronological, so it works as a watermark.
        #
        # We use `>=`, not `>`. Strictly-after would SKIP any claim whose
        # updated_at exactly equals the watermark — and multiple claims can
        # share a timestamp (same-second ingest). Skipping a claim is silent
        # data loss. `>=` instead re-exports the boundary claim(s); the merge
        # engine is idempotent (dedups on idempotency_key + text-hash), so a
        # re-export costs nothing but a few rows. Safe beats clean.
        watermark = since.strip()
        if watermark:
            claim_rows = src.execute(
                "SELECT * FROM claims WHERE updated_at >= ? ORDER BY updated_at",
                (watermark,),
            ).fetchall()
        else:
            claim_rows = src.execute(
                "SELECT * FROM claims ORDER BY updated_at"
            ).fetchall()

        if not claim_rows:
            out.commit()
            return {
                "exported": 0,
                "citations": 0,
                "since": watermark,
                "max_updated_at": None,
            }

        claim_cols = [c[1] for c in src.execute("PRAGMA table_info(claims)").fetchall()]
        placeholders = ",".join("?" for _ in claim_cols)
        col_list = ",".join(claim_cols)
        insert_claim = f"INSERT INTO claims ({col_list}) VALUES ({placeholders})"

        exported_ids: list[int] = []
        max_updated = ""
        for row in claim_rows:
            out.execute(insert_claim, tuple(row[c] for c in claim_cols))
            exported_ids.append(int(row["id"]))
            updated = str(row["updated_at"] or "")
            if updated > max_updated:
                max_updated = updated

        # Citations for exactly the exported claims. claim_id linkage is
        # preserved because we keep original claim ids in the delta file.
        cit_cols = [c[1] for c in src.execute("PRAGMA table_info(citations)").fetchall()]
        cit_placeholders = ",".join("?" for _ in cit_cols)
        cit_col_list = ",".join(cit_cols)
        insert_cit = f"INSERT INTO citations ({cit_col_list}) VALUES ({cit_placeholders})"

        citation_count = 0
        # Chunk the IN clause — SQLite caps host parameters at 999.
        for start in range(0, len(exported_ids), 900):
            batch = exported_ids[start : start + 900]
            qmarks = ",".join("?" for _ in batch)
            cit_rows = src.execute(
                f"SELECT * FROM citations WHERE claim_id IN ({qmarks})",
                batch,
            ).fetchall()
            for cit in cit_rows:
                out.execute(insert_cit, tuple(cit[c] for c in cit_cols))
                citation_count += 1

        out.commit()
        logger.info(
            "export_delta: %d claims, %d citations since %r",
            len(exported_ids),
            citation_count,
            watermark or "(full)",
        )
        return {
            "exported": len(exported_ids),
            "citations": citation_count,
            "since": watermark,
            "max_updated_at": max_updated or None,
        }
    finally:
        src.close()
        out.close()
