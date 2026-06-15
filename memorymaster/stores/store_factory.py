from __future__ import annotations

from pathlib import Path

from memorymaster.stores.storage import SQLiteStore


def is_postgres_dsn(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith("postgres://") or lowered.startswith("postgresql://")


def create_store(db_target: str | Path, *, read_only: bool = False):
    """Build the store for ``db_target``.

    ``read_only`` (P1 WAL-discipline, spec §2.2) puts a SQLite store into
    strict mode=ro + query_only mode so the recall hook can never take a
    write lock. It is a SQLite lock-avoidance mechanism only: Postgres has
    server-side MVCC and no equivalent client mode here, so the flag is
    ignored for Postgres DSNs.
    """
    target = str(db_target)
    if is_postgres_dsn(target):
        from memorymaster.stores.postgres_store import PostgresStore

        return PostgresStore(target)
    return SQLiteStore(Path(target), read_only=read_only)
