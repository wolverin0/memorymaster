"""Additive auxiliary-ledger migrations; never opt historical extractions in."""

import sqlite3

# Every column the numbered steps add, in order (name, ADD COLUMN definition).
_COLUMNS = (
    ("resume_eligible", "resume_eligible INTEGER NOT NULL DEFAULT 0 CHECK(resume_eligible IN (0,1))"),
    ("extraction_run_id", "extraction_run_id TEXT"),
    ("deferred_reason", "deferred_reason TEXT"),
    ("error_count", "error_count INTEGER NOT NULL DEFAULT 0"),
    ("held_count", "held_count INTEGER NOT NULL DEFAULT 0"),
)


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("CREATE TABLE IF NOT EXISTS dream_schema_versions (version INTEGER PRIMARY KEY)")
    if conn.execute("SELECT 1 FROM dream_schema_versions WHERE version=1").fetchone() is None:
        conn.execute("ALTER TABLE dream_captures ADD COLUMN resume_eligible INTEGER NOT NULL DEFAULT 0 CHECK(resume_eligible IN (0,1))")
        conn.execute("ALTER TABLE dream_captures ADD COLUMN extraction_run_id TEXT")
        conn.execute("ALTER TABLE dream_captures ADD COLUMN deferred_reason TEXT")
        conn.execute("INSERT INTO dream_schema_versions VALUES (1)")
    if conn.execute("SELECT 1 FROM dream_schema_versions WHERE version=2").fetchone() is None:
        # Absolute per-capture failure count; stage success never resets it.
        conn.execute("ALTER TABLE dream_captures ADD COLUMN error_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO dream_schema_versions VALUES (2)")
    if conn.execute("SELECT 1 FROM dream_schema_versions WHERE version=3").fetchone() is None:
        # Candidates Jev held (S3 INGEST); retention never prunes such a capture.
        conn.execute("ALTER TABLE dream_captures ADD COLUMN held_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO dream_schema_versions VALUES (3)")
    # A version row is not proof of its column: another build once recorded version 3
    # without held_count and 4.9.0 Dreaming died on "no such column". Repair by column.
    present = {row[1] for row in conn.execute("PRAGMA table_info(dream_captures)")}
    for name, ddl in _COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE dream_captures ADD COLUMN {ddl}")
    conn.commit()
