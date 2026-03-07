from __future__ import annotations

from pathlib import Path

from memorymaster.storage import SQLiteStore


def is_postgres_dsn(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith("postgres://") or lowered.startswith("postgresql://")


def create_store(db_target: str | Path):
    target = str(db_target)
    if is_postgres_dsn(target):
        from memorymaster.postgres_store import PostgresStore

        return PostgresStore(target)
    return SQLiteStore(Path(target))
