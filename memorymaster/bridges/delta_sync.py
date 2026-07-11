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

Every transported claim/citation crosses the canonical persisted sensitivity
envelope. Content is redacted and secret-shaped metadata is omitted fail-closed;
the source database is read-only and remains untouched.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from memorymaster.bridges.persisted_envelope import (
    persisted_claim_id,
    sanitize_claim_envelope,
)
from memorymaster.core.security import SensitiveMetadataError, validate_persisted_metadata
from memorymaster.stores._storage_shared import connect_ro, open_conn
from memorymaster.stores.store_factory import is_postgres_dsn

logger = logging.getLogger(__name__)


# Tables a merge source must carry. The merge engine (db_merge.py) reads
# `claims` (SELECT *) and `citations` (source/locator/excerpt/created_at).
# Nothing else is needed — FTS5, events, entity tables are all rebuilt or
# ignored on the merge side.
_DELTA_TABLES = ("claims", "citations")

_REQUIRED_DELTA_COLUMNS = {
    "claims": frozenset({"id", "text", "updated_at"}),
    "citations": frozenset({"claim_id", "source", "created_at"}),
}


def _quote_identifier(identifier: str) -> str:
    validate_persisted_metadata({"delta_identifier": identifier})
    if not identifier or "\x00" in identifier:
        raise ValueError("Delta schema contains an invalid identifier.")
    return '"' + identifier.replace('"', '""') + '"'


def _sqlite_affinity(declared_type: object) -> str:
    declared = str(declared_type or "").upper()
    if "INT" in declared:
        return "INTEGER"
    if any(token in declared for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if any(token in declared for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    if not declared or "BLOB" in declared:
        return "BLOB"
    return "NUMERIC"


def _paths_alias(source: Path, output: Path) -> bool:
    source_resolved = source.resolve(strict=True)
    output_resolved = output.resolve(strict=False)
    if source_resolved == output_resolved:
        return True
    if output.exists():
        try:
            return source.samefile(output)
        except OSError:
            return False
    return False


def _copy_table_ddl(src: sqlite3.Connection, out: sqlite3.Connection, table: str) -> None:
    """Create a value-only transport table without executing source SQL."""
    row = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None or not row[0]:
        raise ValueError(f"source DB has no '{table}' table — not a memorymaster DB?")
    validate_persisted_metadata(
        {"delta_table_name": table, "delta_table_ddl": row[0]}
    )
    if re.match(r"^\s*CREATE\s+TABLE\b", str(row[0]), re.IGNORECASE) is None:
        raise ValueError("Source table does not have a canonical transport schema.")
    quoted_table = _quote_identifier(table)
    columns = src.execute(f"PRAGMA table_info({quoted_table})").fetchall()
    names = {str(column[1]) for column in columns}
    if not columns or not _REQUIRED_DELTA_COLUMNS[table].issubset(names):
        raise ValueError("Source table does not have a canonical transport schema.")
    definitions = ", ".join(
        f"{_quote_identifier(str(column[1]))} {_sqlite_affinity(column[2])}"
        for column in columns
    )
    out.execute(f"CREATE TABLE {quoted_table} ({definitions})")


def _load_citations_by_claim(
    src: sqlite3.Connection, claim_ids: list[int]
) -> dict[int, list[dict[str, object]]]:
    by_claim: dict[int, list[dict[str, object]]] = {}
    for start in range(0, len(claim_ids), 900):
        batch = claim_ids[start : start + 900]
        qmarks = ",".join("?" for _ in batch)
        rows = src.execute(
            f"SELECT * FROM citations WHERE claim_id IN ({qmarks})",
            batch,
        ).fetchall()
        for row in rows:
            by_claim.setdefault(int(row["claim_id"]), []).append(dict(row))
    return by_claim


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
        dict with ``exported``/``citations`` counts, ``rejected`` metadata
        rejections, ``redacted`` claim envelopes, ``since`` (echoed watermark),
        and ``max_updated_at`` (the newest safely exported ``updated_at``;
        None when the delta is empty).

    Raises:
        FileNotFoundError: source DB missing.
        ValueError: source DB lacks the expected tables.
    """
    if is_postgres_dsn(str(source_db)) or is_postgres_dsn(str(output_path)):
        raise ValueError(
            "export-delta supports SQLite paths only; raw Postgres team deltas are disabled."
        )

    source_path = Path(str(source_db))
    output_path = Path(output_path)
    if not source_path.exists():
        raise FileNotFoundError(f"Source DB not found: {source_path}")
    watermark = since.strip()
    validate_persisted_metadata({"delta_since": watermark})
    if _paths_alias(source_path, output_path):
        raise ValueError("Delta output must not alias the source database.")

    # Fresh output file every time — a stale delta would merge old rows again
    # (harmless thanks to idempotent merge, but wasteful).
    if output_path.exists():
        output_path.unlink()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Source is read-only (connect_ro takes no lock on the live DB); the
    # fresh delta file gets the uniform writer envelope.
    src = connect_ro(str(source_path))
    out = open_conn(output_path)
    # The delta is a value-only transport file, not a live DB. Its tables are
    # synthesized from source column names/affinities; untrusted source DDL,
    # constraints, triggers, and cross-window foreign keys are never executed.
    out.execute("PRAGMA foreign_keys=OFF")
    try:
        src.execute("BEGIN")
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
                "rejected": 0,
                "redacted": 0,
                "since": watermark,
                "max_updated_at": None,
            }

        claim_cols = [c[1] for c in src.execute("PRAGMA table_info(claims)").fetchall()]
        placeholders = ",".join("?" for _ in claim_cols)
        col_list = ",".join(_quote_identifier(str(column)) for column in claim_cols)
        insert_claim = f"INSERT INTO claims ({col_list}) VALUES ({placeholders})"

        candidate_ids = [persisted_claim_id(dict(row)) for row in claim_rows]
        citations_by_claim = _load_citations_by_claim(src, candidate_ids)
        cit_cols = [c[1] for c in src.execute("PRAGMA table_info(citations)").fetchall()]
        cit_placeholders = ",".join("?" for _ in cit_cols)
        cit_col_list = ",".join(_quote_identifier(str(column)) for column in cit_cols)
        insert_cit = f"INSERT INTO citations ({cit_col_list}) VALUES ({cit_placeholders})"

        exported = 0
        citation_count = 0
        rejected = 0
        redacted = 0
        max_updated = ""
        for row in claim_rows:
            claim_id = persisted_claim_id(dict(row))
            try:
                envelope = sanitize_claim_envelope(
                    dict(row), citations_by_claim.get(claim_id, [])
                )
            except SensitiveMetadataError as exc:
                rejected += 1
                logger.warning(
                    "export_delta: rejected claim id=%d unsafe field=%s findings=%s",
                    claim_id,
                    exc.field,
                    ",".join(exc.findings),
                )
                continue
            out.execute(insert_claim, tuple(envelope.row[c] for c in claim_cols))
            for citation in envelope.citations:
                out.execute(insert_cit, tuple(citation[c] for c in cit_cols))
                citation_count += 1
            exported += 1
            redacted += int(bool(envelope.findings))
            updated = str(envelope.row.get("updated_at") or "")
            if updated > max_updated:
                max_updated = updated

        out.commit()
        logger.info(
            "export_delta: %d claims, %d citations, %d rejected since %r",
            exported,
            citation_count,
            rejected,
            watermark or "(full)",
        )
        return {
            "exported": exported,
            "citations": citation_count,
            "rejected": rejected,
            "redacted": redacted,
            "since": watermark,
            "max_updated_at": max_updated or None,
        }
    finally:
        src.close()
        out.close()
