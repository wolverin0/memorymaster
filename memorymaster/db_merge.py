"""Bidirectional DB merge — import claims from a remote memorymaster DB.

Merges claims from a source DB into the local DB without duplicating.
Uses idempotency_key + text hash for dedup. Preserves both sides' claims.

Usage:
    memorymaster merge-db --source /path/to/remote.db
    memorymaster merge-db --source user@remote-host:/opt/memorymaster/memorymaster.db
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def _text_hash(text: str) -> str:
    """Deterministic hash for claim dedup when no idempotency_key exists."""
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def _build_insert_values(
    row: sqlite3.Row, common_cols: list[str], ikey: str
) -> tuple[list[str], list[object]]:
    """Build column names and values for claim insertion."""
    cols_to_insert = []
    values = []
    for col in common_cols:
        if col == "idempotency_key":
            values.append(ikey)
        else:
            values.append(row[col] if col in row.keys() else None)
        cols_to_insert.append(col)
    return cols_to_insert, values


def _copy_claim_citations(src: sqlite3.Connection, tgt: sqlite3.Connection, old_id: int, new_id: int) -> None:
    """Copy citations from source claim to target claim."""
    try:
        cites = src.execute(
            "SELECT source, locator, excerpt, created_at FROM citations WHERE claim_id = ?",
            (old_id,),
        ).fetchall()
        for cite in cites:
            tgt.execute(
                "INSERT INTO citations (claim_id, source, locator, excerpt, created_at) VALUES (?, ?, ?, ?, ?)",
                (new_id, cite["source"], cite["locator"], cite["excerpt"], cite["created_at"]),
            )
    except sqlite3.OperationalError:
        pass  # citations table might differ


def _insert_claim_into_target(
    row: sqlite3.Row,
    common_cols: list[str],
    ikey: str,
    text: str,
    src: sqlite3.Connection,
    tgt: sqlite3.Connection,
) -> bool:
    """Insert a single claim into target DB and copy citations. Returns True if successful."""
    try:
        cols_to_insert, values = _build_insert_values(row, common_cols, ikey)
        placeholders = ",".join("?" for _ in cols_to_insert)
        col_names = ",".join(cols_to_insert)

        tgt.execute(
            f"INSERT INTO claims ({col_names}) VALUES ({placeholders})",
            values,
        )
        new_id = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]

        _copy_claim_citations(src, tgt, row["id"], new_id)
        return True
    except Exception as exc:
        logger.warning("Failed to merge claim: %s", exc)
        return False


def merge_databases(target_db: str, source_db: str) -> dict[str, int]:
    """Merge claims from source_db into target_db.

    Skips claims that already exist (matched by idempotency_key or text hash).
    Copies citations for newly merged claims.

    Returns dict with: scanned, merged, skipped, errors
    """
    stats = {"scanned": 0, "merged": 0, "skipped": 0, "errors": 0}

    if not Path(source_db).exists():
        raise FileNotFoundError(f"Source DB not found: {source_db}")

    src = sqlite3.connect(source_db)
    src.row_factory = sqlite3.Row
    tgt = sqlite3.connect(target_db)
    tgt.row_factory = sqlite3.Row

    try:
        # Build set of existing claim fingerprints in target
        existing_keys: set[str] = set()
        existing_hashes: set[str] = set()

        for row in tgt.execute("SELECT idempotency_key, text FROM claims").fetchall():
            if row["idempotency_key"]:
                existing_keys.add(row["idempotency_key"])
            existing_hashes.add(_text_hash(row["text"]))

        # Get all columns from source claims table
        src_cols = [col[1] for col in src.execute("PRAGMA table_info(claims)").fetchall()]
        # Filter to columns that exist in target
        tgt_cols = {col[1] for col in tgt.execute("PRAGMA table_info(claims)").fetchall()}
        common_cols = [c for c in src_cols if c in tgt_cols and c != "id"]

        # Scan source claims
        source_claims = src.execute("SELECT * FROM claims WHERE status != 'archived'").fetchall()

        for row in source_claims:
            stats["scanned"] += 1
            ikey = row["idempotency_key"] if "idempotency_key" in row.keys() else None
            text = row["text"]

            # Skip if already exists
            if ikey and ikey in existing_keys:
                stats["skipped"] += 1
                continue
            if _text_hash(text) in existing_hashes:
                stats["skipped"] += 1
                continue

            # Build idempotency key if missing
            if not ikey:
                ikey = f"merge-{_text_hash(text)}"

            # Insert into target
            if _insert_claim_into_target(row, common_cols, ikey, text, src, tgt):
                existing_keys.add(ikey)
                existing_hashes.add(_text_hash(text))
                stats["merged"] += 1
            else:
                stats["errors"] += 1

        tgt.commit()

    finally:
        src.close()
        tgt.close()

    logger.info(
        "Merge complete: %d scanned, %d merged, %d skipped, %d errors",
        stats["scanned"], stats["merged"], stats["skipped"], stats["errors"],
    )
    return stats
