"""Index the active skill catalog without scanning ordinary claim payloads."""
from __future__ import annotations

VERSION = 25
DESCRIPTION = "Covering partial index for active governed skill enumeration"

_SQL = """
CREATE INDEX IF NOT EXISTS idx_claims_active_skill_catalog
ON claims(status, id, tenant_id, scope)
WHERE claim_type = 'skill' AND status = 'confirmed' AND replaced_by_claim_id IS NULL
"""


def apply_sqlite(conn) -> None:
    # The framework also permits migration discovery/stamping on an empty DB.
    # schema.sql supplies this index when init_db later creates the tables.
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'").fetchone()
    if exists:
        conn.execute(_SQL)
    conn.commit()


def apply_postgres(conn) -> None:
    raise RuntimeError("migration 25 is SQLite-only; governed skill catalog on PostgreSQL is deferred")
