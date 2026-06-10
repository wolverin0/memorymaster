from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path

from memorymaster._storage_lifecycle import _LifecycleMixin
from memorymaster._storage_read import _ReadMixin
from memorymaster._storage_schema import _SchemaMixin
# Re-export shared helpers for backward compat with external imports.
from memorymaster._storage_shared import (
    EVENT_HASH_ALGO as EVENT_HASH_ALGO,
    connect_ro as connect_ro,
    generate_human_id_hash as generate_human_id_hash,
    generate_top_level_human_id as generate_top_level_human_id,
    open_conn as open_conn,
    utc_now as utc_now,
)
from memorymaster._storage_sources import _SourceItemsMixin
from memorymaster._storage_write_claims import _WriteClaimsMixin
from memorymaster.schema import load_schema_sql

logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_HASH_ALGO",
    "SQLiteStore",
    "connect_ro",
    "generate_human_id_hash",
    "generate_top_level_human_id",
    "open_conn",
    "utc_now",
]


class SQLiteStore(_SchemaMixin, _ReadMixin, _WriteClaimsMixin, _LifecycleMixin, _SourceItemsMixin):
    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        self.db_path = str(db_path)
        # P1 WAL-discipline (spec §2.2): an RO store hands out mode=ro +
        # query_only connections from connect(), so every read path — and
        # every side lookup that goes through the store — follows
        # automatically, and the store can NEVER take a write lock. Write
        # attempts raise sqlite3.OperationalError instead of silently
        # contending with the fleet's writers.
        self.read_only = bool(read_only)
    def connect(self) -> sqlite3.Connection:
        # Delegates to the canonical helper so every writer in the fleet
        # shares one pragma envelope (WAL + foreign_keys + busy_timeout=15000,
        # up from the historical 5000ms — strictly safer under contention).
        # See _storage_shared.open_conn for the lost-write race rationale.
        if self.read_only:
            return connect_ro(self.db_path)
        return open_conn(self.db_path)

    def connect_ro(self) -> sqlite3.Connection:
        """Read-only connection that cannot take a write lock (mode=ro + query_only)."""
        return connect_ro(self.db_path)
    def init_db(self) -> None:
        if self.read_only:
            # Fail loudly: a read-only store must never mutate schema. The
            # lenient executescript fallback below would otherwise swallow
            # the per-statement OperationalErrors and "succeed" silently.
            raise sqlite3.OperationalError(
                "read-only store cannot init_db; construct without read_only=True"
            )
        with self.connect() as conn:
            try:
                conn.executescript(load_schema_sql())
                conn.commit()
            except sqlite3.Error as e:
                # If executescript fails (e.g., due to partial initialization),
                # rollback and try a more lenient approach
                logger.warning("executescript failed, attempting lenient schema initialization: %s", e)
                conn.rollback()

                # Split the schema into individual statements and execute them,
                # ignoring errors for already-existing objects
                schema_sql = load_schema_sql()
                statements = [stmt.strip() for stmt in schema_sql.split(';') if stmt.strip()]
                for stmt in statements:
                    with contextlib.suppress(sqlite3.OperationalError):
                        conn.execute(stmt)
                conn.commit()

            self._ensure_claim_idempotency_schema(conn)
            self._ensure_confirmed_tuple_uniqueness_schema(conn)
            self._ensure_event_integrity_schema(conn)
            self._ensure_fts5_schema(conn)
            self._ensure_claim_links_schema(conn)
            self._ensure_human_id_schema(conn)
            self._ensure_tenant_id_schema(conn)
            self._ensure_temporal_columns(conn)
            self._ensure_tiering_columns(conn)
            self._ensure_agent_columns(conn)
            self._ensure_binding_columns(conn)
            self._ensure_version_column(conn)
            self._ensure_embeddings_schema(conn)
            self._ensure_atlas_source_schema(conn)
            # Entity registry (GBrain-inspired canonical entities + alias resolution)
            from memorymaster.entity_registry import ensure_entity_schema
            ensure_entity_schema(conn)
            try:
                conn.execute("ALTER TABLE claims ADD COLUMN entity_id INTEGER")
            except sqlite3.OperationalError:
                pass  # already exists
            conn.commit()

        # v3.20.0-S1: apply versioned migrations after the legacy init flow.
        # The 0001 baseline is a no-op (existing schema IS the baseline);
        # any future migrations (0002+) get applied on top here. Opens a
        # fresh connection rather than reusing the one above because the
        # migration runner manages its own transactions per-step.
        from memorymaster.migrations import MigrationRunner

        with self.connect() as mig_conn:
            MigrationRunner(mig_conn, backend="sqlite").apply_pending()
