"""Add durable, privacy-preserving session-to-scope bindings."""
from __future__ import annotations

from typing import Any


VERSION = 19
DESCRIPTION = "Add durable session-to-scope bindings"


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_scope_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_hash TEXT NOT NULL CHECK(length(session_hash) = 64),
    source_agent TEXT NOT NULL,
    platform TEXT NOT NULL,
    scope TEXT NOT NULL,
    workspace_slug TEXT,
    task_label TEXT,
    binding_source TEXT NOT NULL CHECK(
        binding_source IN ('explicit', 'verified_workspace', 'default_user')
    ),
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_scope_active_identity
    ON session_scope_bindings(session_hash, source_agent, platform)
    WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_session_scope_active_expiry
    ON session_scope_bindings(expires_at, ended_at);
CREATE INDEX IF NOT EXISTS idx_session_scope_scope
    ON session_scope_bindings(scope, ended_at);
"""


def apply_sqlite(conn: Any) -> None:
    conn.executescript(_SQLITE_SCHEMA)
    conn.commit()


def apply_postgres(conn: Any) -> None:
    """Fail closed because Postgres rollout was explicitly deferred."""
    raise RuntimeError("migration 19 is SQLite-only; Postgres rollout is deferred")
