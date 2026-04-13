from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.retry import connect_with_retry
from memorymaster.embeddings import EmbeddingProvider, cosine_similarity
from memorymaster.models import (
    CLAIM_LINK_TYPES,
    CLAIM_STATUSES,
    STATUS_TRANSITION_EVENT_TYPES,
    Citation,
    CitationInput,
    Claim,
    ClaimLink,
    Event,
    validate_event_payload,
    validate_event_type,
    validate_transition_event_type,
)
from memorymaster.schema import load_schema_sql

logger = logging.getLogger(__name__)

# Re-export shared helpers for backward compat with external imports
from memorymaster._storage_shared import (
    EVENT_HASH_ALGO,
    HUMAN_ID_PREFIX,
    SQLITE_CONFIRMED_TUPLE_GUARD_TRIGGERS,
    SQLITE_EVENTS_APPEND_ONLY_TRIGGERS,
    ConcurrentModificationError,
    generate_human_id_hash,
    generate_top_level_human_id,
    utc_now,
)


from memorymaster._storage_schema import _SchemaMixin
from memorymaster._storage_read import _ReadMixin
from memorymaster._storage_write_claims import _WriteClaimsMixin
from memorymaster._storage_lifecycle import _LifecycleMixin


class SQLiteStore(_SchemaMixin, _ReadMixin, _WriteClaimsMixin, _LifecycleMixin):
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
    def connect(self) -> sqlite3.Connection:
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            return conn

        return connect_with_retry(_open)
    def init_db(self) -> None:
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
            # Entity registry (GBrain-inspired canonical entities + alias resolution)
            from memorymaster.entity_registry import ensure_entity_schema
            ensure_entity_schema(conn)
            try:
                conn.execute("ALTER TABLE claims ADD COLUMN entity_id INTEGER")
            except sqlite3.OperationalError:
                pass  # already exists
            conn.commit()
