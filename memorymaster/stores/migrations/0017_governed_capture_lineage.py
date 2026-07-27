"""Add replay-safe capture jobs and exact evidence/graph lineage."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

VERSION = 17
DESCRIPTION = "Governed capture jobs and relational lineage"

logger = logging.getLogger(__name__)
_EVIDENCE_LOCATOR = re.compile(r"^evidence:(\d+)$")

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS claim_evidence_links (
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    evidence_item_id INTEGER NOT NULL REFERENCES evidence_items(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'support',
    created_at TEXT NOT NULL,
    PRIMARY KEY (claim_id, evidence_item_id, role)
);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_evidence
    ON claim_evidence_links(evidence_item_id);

CREATE TABLE IF NOT EXISTS capture_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_item_id INTEGER NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('extract_text','extract_claims','extract_graph')),
    status TEXT NOT NULL CHECK (status IN ('pending','leased','retryable','blocked','completed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0 AND attempts <= 5),
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    error_code TEXT,
    error_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (source_item_id, content_hash, stage)
);
CREATE INDEX IF NOT EXISTS idx_capture_jobs_due
    ON capture_jobs(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_capture_jobs_lease
    ON capture_jobs(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS entity_edge_supports (
    source_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    supporting_claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    ontology_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (
        source_entity_id, target_entity_id, relation,
        supporting_claim_id, scope, ontology_version
    )
);
CREATE INDEX IF NOT EXISTS idx_edge_supports_claim
    ON entity_edge_supports(supporting_claim_id);
CREATE INDEX IF NOT EXISTS idx_edge_supports_scope
    ON entity_edge_supports(scope, source_entity_id, target_entity_id);
"""

_POSTGRES_STATEMENTS = (
    "ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS content_hash TEXT",
    "ALTER TABLE source_items ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ",
    "ALTER TABLE source_items ADD COLUMN IF NOT EXISTS retirement_reason TEXT",
    """CREATE TABLE IF NOT EXISTS claim_evidence_links (
        claim_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
        evidence_item_id BIGINT NOT NULL REFERENCES evidence_items(id) ON DELETE CASCADE,
        role TEXT NOT NULL DEFAULT 'support',
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (claim_id, evidence_item_id, role))""",
    "CREATE INDEX IF NOT EXISTS idx_claim_evidence_evidence ON claim_evidence_links(evidence_item_id)",
    """CREATE TABLE IF NOT EXISTS capture_jobs (
        id BIGSERIAL PRIMARY KEY,
        source_item_id BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
        content_hash TEXT NOT NULL,
        stage TEXT NOT NULL CHECK (stage IN ('extract_text','extract_claims','extract_graph')),
        status TEXT NOT NULL CHECK (status IN ('pending','leased','retryable','blocked','completed','cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0 AND attempts <= 5),
        next_attempt_at TIMESTAMPTZ,
        lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ,
        error_code TEXT,
        error_detail TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ,
        UNIQUE (source_item_id, content_hash, stage))""",
    "CREATE INDEX IF NOT EXISTS idx_capture_jobs_due ON capture_jobs(status, next_attempt_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_capture_jobs_lease ON capture_jobs(status, lease_expires_at)",
    """CREATE TABLE IF NOT EXISTS entity_edge_supports (
        source_entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        target_entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        relation TEXT NOT NULL,
        supporting_claim_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
        scope TEXT NOT NULL,
        ontology_version TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (
            source_entity_id, target_entity_id, relation,
            supporting_claim_id, scope, ontology_version))""",
    "CREATE INDEX IF NOT EXISTS idx_edge_supports_claim ON entity_edge_supports(supporting_claim_id)",
    """CREATE INDEX IF NOT EXISTS idx_edge_supports_scope
       ON entity_edge_supports(scope, source_entity_id, target_entity_id)""",
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row_value(row: Any, name: str, index: int) -> Any:
    if isinstance(row, dict):
        return row[name]
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return row[index]


def _sqlite_add_column(conn: Any, table: str, definition: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
    except Exception as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def _sqlite_table_exists(conn: Any, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _hash_existing_evidence_sqlite(conn: Any) -> int:
    changed = 0
    rows = conn.execute(
        "SELECT id, text FROM evidence_items WHERE content_hash IS NULL AND text IS NOT NULL"
    ).fetchall()
    for row in rows:
        digest = hashlib.sha256(str(_row_value(row, "text", 1)).encode("utf-8")).hexdigest()
        conn.execute(
            "UPDATE evidence_items SET content_hash = ? WHERE id = ? AND content_hash IS NULL",
            (digest, int(_row_value(row, "id", 0))),
        )
        changed += 1
    return changed


def _fetchall(cursor: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    result = cursor.execute(sql, params)
    return (result or cursor).fetchall()


def _fetchone(cursor: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    result = cursor.execute(sql, params)
    return (result or cursor).fetchone()


def _backfill_evidence_links(
    cursor: Any,
    *,
    backend: str,
    citations: list[Any],
    apply: bool,
    stamp: str,
    report: dict[str, int],
) -> None:
    placeholder = "?" if backend == "sqlite" else "%s"
    for row in citations:
        claim_id = int(_row_value(row, "claim_id", 0))
        locator = str(_row_value(row, "locator", 1))
        match = _EVIDENCE_LOCATOR.fullmatch(locator)
        if match is None:
            if locator.startswith("evidence:"):
                report["ambiguous_locators"] += 1
            continue
        evidence_id = int(match.group(1))
        exists = _fetchone(
            cursor, f"SELECT 1 FROM evidence_items WHERE id = {placeholder}", (evidence_id,)
        )
        if exists is None:
            report["missing_evidence"] += 1
            continue
        report["exact_evidence_links"] += 1
        if apply:
            if backend == "sqlite":
                cursor.execute(
                    """INSERT OR IGNORE INTO claim_evidence_links
                       (claim_id, evidence_item_id, role, created_at)
                       VALUES (?, ?, 'support', ?)""",
                    (claim_id, evidence_id, stamp),
                )
            else:
                cursor.execute(
                    """INSERT INTO claim_evidence_links
                       (claim_id, evidence_item_id, role, created_at)
                       VALUES (%s, %s, 'support', %s)
                       ON CONFLICT DO NOTHING""",
                    (claim_id, evidence_id, stamp),
                )


def _backfill_edge_supports(
    cursor: Any,
    *,
    backend: str,
    edges: list[Any],
    apply: bool,
    stamp: str,
    report: dict[str, int],
) -> None:
    placeholder = "?" if backend == "sqlite" else "%s"
    for row in edges:
        source_id = int(_row_value(row, "source_id", 0))
        target_id = int(_row_value(row, "target_id", 1))
        relation = str(_row_value(row, "relation", 2))
        claim_id = int(_row_value(row, "claim_id", 3))
        claim = _fetchone(
            cursor, f"SELECT scope FROM claims WHERE id = {placeholder}", (claim_id,)
        )
        if claim is None:
            report["missing_edge_claims"] += 1
            continue
        scope = str(_row_value(claim, "scope", 0))
        report["exact_edge_supports"] += 1
        if apply:
            values = (source_id, target_id, relation, claim_id, scope, "legacy-v0", stamp)
            if backend == "sqlite":
                cursor.execute(
                    """INSERT OR IGNORE INTO entity_edge_supports
                       (source_entity_id, target_entity_id, relation,
                        supporting_claim_id, scope, ontology_version, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
            else:
                cursor.execute(
                    """INSERT INTO entity_edge_supports
                       (source_entity_id, target_entity_id, relation,
                        supporting_claim_id, scope, ontology_version, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    values,
                )


def backfill_lineage(conn: Any, *, backend: str, apply: bool) -> dict[str, int]:
    """Backfill only exact ``evidence:<id>`` and existing edge claim references."""
    if backend not in {"sqlite", "postgres"}:
        raise ValueError("backend must be sqlite or postgres")
    cursor = conn if backend == "sqlite" else conn.cursor()
    citations = _fetchall(
        cursor, "SELECT claim_id, locator FROM citations WHERE locator IS NOT NULL"
    )
    try:
        edges = _fetchall(
            cursor,
            """SELECT source_id, target_id, relation, claim_id
               FROM entity_edges WHERE claim_id IS NOT NULL""",
        )
    except Exception:
        edges = []
    report = {
        "citation_rows": len(citations),
        "exact_evidence_links": 0,
        "missing_evidence": 0,
        "ambiguous_locators": 0,
        "edge_rows": len(edges),
        "exact_edge_supports": 0,
        "missing_edge_claims": 0,
    }
    stamp = _now()
    _backfill_evidence_links(
        cursor, backend=backend, citations=citations, apply=apply, stamp=stamp, report=report
    )
    _backfill_edge_supports(
        cursor, backend=backend, edges=edges, apply=apply, stamp=stamp, report=report
    )
    return report


def apply_sqlite(conn: Any) -> None:
    if not all(
        _sqlite_table_exists(conn, table)
        for table in ("claims", "source_items", "evidence_items", "entities")
    ):
        # The standalone migration CLI is allowed to stamp an empty database.
        # SQLiteStore.init_db performs post-migration convergence once the
        # baseline schema exists.
        conn.commit()
        return
    _sqlite_add_column(conn, "evidence_items", "content_hash TEXT")
    _sqlite_add_column(conn, "source_items", "retired_at TEXT")
    _sqlite_add_column(conn, "source_items", "retirement_reason TEXT")
    conn.executescript(_SQLITE_DDL)
    hashed = _hash_existing_evidence_sqlite(conn)
    report = backfill_lineage(conn, backend="sqlite", apply=True)
    conn.commit()
    logger.info("capture lineage migration: evidence_hashed=%d report=%s", hashed, report)


def apply_postgres(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS count FROM information_schema.tables
               WHERE table_schema=current_schema()
                 AND table_name IN ('claims','source_items','evidence_items','entities')"""
        )
        row = cur.fetchone()
        count = int(_row_value(row, "count", 0)) if row is not None else 0
    if count != 4:
        conn.commit()
        return
    with conn.cursor() as cur:
        for statement in _POSTGRES_STATEMENTS:
            cur.execute(statement)
    report = backfill_lineage(conn, backend="postgres", apply=True)
    conn.commit()
    logger.info("capture lineage migration: report=%s", report)
