from __future__ import annotations

from pathlib import Path
from typing import Iterable

from memorymaster.core.provider_health import register_store_sink
from memorymaster.stores.storage import SQLiteStore


def is_postgres_dsn(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith("postgres://") or lowered.startswith("postgresql://")


def create_store(
    db_target: str | Path,
    *,
    read_only: bool = False,
    tenant_id: str | None = None,
    require_tenant: bool = False,
    principal: str | None = None,
    allowed_scopes: Iterable[str] | None = None,
):
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

        store = PostgresStore(
            target.strip(),
            tenant_id=tenant_id,
            require_tenant=require_tenant,
            principal=principal,
            allowed_scopes=allowed_scopes,
        )
    else:
        store = SQLiteStore(Path(target), read_only=read_only)

    if not read_only:
        # Give this process a durable producer for LLM provider failures
        # (inert-signals R10). `llm_provider` holds no store handle, and
        # observability counters are in-memory only, so without a sink the
        # operational health check -- which usually runs in a DIFFERENT process
        # -- has nothing to find and reports 0 forever. Registering here covers
        # every LLM caller at once instead of threading a handle through ~25
        # call sites. Read-only stores cannot write and are left unregistered.
        register_store_sink(store)
    return store
