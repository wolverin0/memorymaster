"""Additive auxiliary-ledger migrations; never opt historical extractions in."""

import sqlite3

# Numbered steps and the columns each adds (name, ADD COLUMN definition).
_STEPS = (
    (1, (("resume_eligible", "resume_eligible INTEGER NOT NULL DEFAULT 0 CHECK(resume_eligible IN (0,1))"),
         ("extraction_run_id", "extraction_run_id TEXT"),
         ("deferred_reason", "deferred_reason TEXT"))),
    # Absolute per-capture failure count; stage success never resets it.
    (2, (("error_count", "error_count INTEGER NOT NULL DEFAULT 0"),)),
    # Candidates Jev held (S3 INGEST); retention bounds how long such a capture is kept.
    (3, (("held_count", "held_count INTEGER NOT NULL DEFAULT 0"),)),
)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring ``dream_captures`` to the current columns, trusting the columns, not the rows.

    Version rows and columns have drifted both ways in production: version 3 recorded
    without ``held_count`` (4.9.0 Dreaming died on "no such column"), and a column
    present without its row would die on "duplicate column name". Each step adds only
    the columns that are missing, and every recorded version ends with its columns.
    """
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("CREATE TABLE IF NOT EXISTS dream_schema_versions (version INTEGER PRIMARY KEY)")
    present = {row[1] for row in conn.execute("PRAGMA table_info(dream_captures)")}
    recorded = {row[0] for row in conn.execute("SELECT version FROM dream_schema_versions")}
    for version, columns in _STEPS:
        for name, ddl in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE dream_captures ADD COLUMN {ddl}")
                present.add(name)
        if version not in recorded:
            conn.execute("INSERT INTO dream_schema_versions VALUES (?)", (version,))
    conn.commit()
