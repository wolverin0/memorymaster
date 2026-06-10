"""Versioned schema migrations for MemoryMaster (v3.20.0-S1).

Pre-v3.20 schema evolved via opportunistic ``ALTER TABLE`` with try/except
sprinkled across storage modules — no version tracking, no rollback, silent
drift risk between SQLite and Postgres. This package replaces that pattern
with explicit versioned migrations.

Each migration is a Python module under this package named
``NNNN_short_description.py`` with four module-level attributes:

    VERSION: int                            # 1, 2, 3, ...
    DESCRIPTION: str                        # one-line human summary
    def apply_sqlite(conn) -> None: ...     # SQLite-backend DDL
    def apply_postgres(conn) -> None: ...   # Postgres-backend DDL

The ``MigrationRunner`` discovers all migration modules, sorts by VERSION,
and applies only the ones not yet recorded in the ``schema_versions``
table. Each applied migration's file source is sha256-checksummed and
stored alongside its version; on subsequent runs, a mismatch raises
``MigrationDriftError`` — migrations are immutable once applied.

P1 init_db fast-path note (spec §2.9): the discovered migration set —
every (VERSION, file checksum) pair — feeds ``storage.schema_stamp()``,
the ``PRAGMA user_version`` fingerprint behind
``MEMORYMASTER_INITDB_FASTPATH``. Adding ANY new migration file therefore
bumps the stamp automatically and forces the next init_db onto the full
path; no manual stamp constant exists to forget.

Public API:

    from memorymaster.migrations import MigrationRunner, MigrationDriftError
    runner = MigrationRunner(conn, backend="sqlite")
    runner.apply_pending()
    runner.status()  # -> list of (version, description, applied_at, status)
"""
from __future__ import annotations

from memorymaster.migrations.runner import (
    MigrationDriftError,
    MigrationRunner,
    discover_migrations,
)

__all__ = [
    "MigrationDriftError",
    "MigrationRunner",
    "discover_migrations",
]
