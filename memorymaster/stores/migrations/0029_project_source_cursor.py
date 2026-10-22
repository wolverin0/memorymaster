"""Track per-claim project-source changes without reading source payloads.

The project projection uses this cursor to read only the claims whose source
rows changed since its previous SessionStart.  Timestamps are not a safe
watermark: a transaction may commit after a read while retaining an older
``updated_at`` value.  The clock therefore advances inside the source-row
transaction and leaves a latest-sequence tombstone for deleted claims.
"""

from __future__ import annotations

from typing import Any


VERSION = 29
DESCRIPTION = "SQLite project-source per-claim change cursor"
# This adds only derived metadata and triggers.  Claim and citation data remain
# valid across a v28/v29 bridge exchange.
MERGE_COMPATIBLE = True


_SQLITE = """
CREATE TABLE IF NOT EXISTS project_source_clock (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    epoch TEXT NOT NULL,
    seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS project_source_changes (
    claim_id INTEGER PRIMARY KEY,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_source_changes_seq
    ON project_source_changes(seq, claim_id);

INSERT OR IGNORE INTO project_source_clock(singleton, epoch, seq)
VALUES (1, lower(hex(randomblob(16))), 0);

CREATE INDEX IF NOT EXISTS idx_project_claims_updated
    ON claims(tenant_id, scope, updated_at, id);
CREATE INDEX IF NOT EXISTS idx_project_claims_valid_from
    ON claims(tenant_id, scope, valid_from, id);
CREATE INDEX IF NOT EXISTS idx_project_claims_valid_until
    ON claims(tenant_id, scope, valid_until, id);
CREATE INDEX IF NOT EXISTS idx_project_citations_created
    ON citations(created_at, claim_id);
CREATE INDEX IF NOT EXISTS idx_project_citations_claim
    ON citations(claim_id, id);

CREATE TRIGGER IF NOT EXISTS project_source_claims_ai AFTER INSERT ON claims BEGIN
    UPDATE project_source_clock SET seq = seq + 1 WHERE singleton = 1;
    INSERT INTO project_source_changes(claim_id, seq)
        SELECT NEW.id, seq FROM project_source_clock WHERE singleton = 1
        ON CONFLICT(claim_id) DO UPDATE SET seq = excluded.seq;
END;
CREATE TRIGGER IF NOT EXISTS project_source_claims_au AFTER UPDATE ON claims BEGIN
    UPDATE project_source_clock SET seq = seq + 1 WHERE singleton = 1;
    INSERT INTO project_source_changes(claim_id, seq)
        SELECT OLD.id, seq FROM project_source_clock WHERE singleton = 1
        ON CONFLICT(claim_id) DO UPDATE SET seq = excluded.seq;
    INSERT INTO project_source_changes(claim_id, seq)
        SELECT NEW.id, seq FROM project_source_clock WHERE singleton = 1
        ON CONFLICT(claim_id) DO UPDATE SET seq = excluded.seq;
END;
CREATE TRIGGER IF NOT EXISTS project_source_claims_ad AFTER DELETE ON claims BEGIN
    UPDATE project_source_clock SET seq = seq + 1 WHERE singleton = 1;
    INSERT INTO project_source_changes(claim_id, seq)
        SELECT OLD.id, seq FROM project_source_clock WHERE singleton = 1
        ON CONFLICT(claim_id) DO UPDATE SET seq = excluded.seq;
END;

CREATE TRIGGER IF NOT EXISTS project_source_citations_ai AFTER INSERT ON citations BEGIN
    UPDATE project_source_clock SET seq = seq + 1 WHERE singleton = 1;
    INSERT INTO project_source_changes(claim_id, seq)
        SELECT NEW.claim_id, seq FROM project_source_clock WHERE singleton = 1
        ON CONFLICT(claim_id) DO UPDATE SET seq = excluded.seq;
END;
CREATE TRIGGER IF NOT EXISTS project_source_citations_au AFTER UPDATE ON citations BEGIN
    UPDATE project_source_clock SET seq = seq + 1 WHERE singleton = 1;
    INSERT INTO project_source_changes(claim_id, seq)
        SELECT OLD.claim_id, seq FROM project_source_clock WHERE singleton = 1
        ON CONFLICT(claim_id) DO UPDATE SET seq = excluded.seq;
    INSERT INTO project_source_changes(claim_id, seq)
        SELECT NEW.claim_id, seq FROM project_source_clock WHERE singleton = 1
        ON CONFLICT(claim_id) DO UPDATE SET seq = excluded.seq;
END;
CREATE TRIGGER IF NOT EXISTS project_source_citations_ad AFTER DELETE ON citations BEGIN
    UPDATE project_source_clock SET seq = seq + 1 WHERE singleton = 1;
    INSERT INTO project_source_changes(claim_id, seq)
        SELECT OLD.claim_id, seq FROM project_source_clock WHERE singleton = 1
        ON CONFLICT(claim_id) DO UPDATE SET seq = excluded.seq;
END;
"""


def _has_table(conn: Any, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def apply_sqlite(conn: Any) -> None:
    """Install the cursor only when both authoritative source tables exist."""
    # The migration framework can stamp an empty database before schema.sql
    # creates its tables.  Creating half of this cursor would be misleading and
    # cannot safely track citation writes, so leave such databases untouched.
    if not _has_table(conn, "claims") or not _has_table(conn, "citations"):
        return
    conn.executescript(_SQLITE)
    conn.commit()


def apply_postgres(conn: Any) -> None:
    """Fail closed until a PostgreSQL source-cursor contract exists."""
    del conn
    raise RuntimeError("migration 29 is SQLite-only; project source cursor is SQLite-only")
