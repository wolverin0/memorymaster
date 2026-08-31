"""Independent root-session support for governed rule and skill candidates."""

from __future__ import annotations

from typing import Any


VERSION = 24
DESCRIPTION = "Track independent root-session lineage for behavioral rules"

_SQLITE = """
CREATE TABLE IF NOT EXISTS rule_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_fingerprint TEXT NOT NULL,
    provider TEXT NOT NULL,
    root_session_hash TEXT NOT NULL,
    project_scope TEXT NOT NULL,
    session_kind TEXT NOT NULL CHECK(session_kind IN ('human','mixed','subagent','automation')),
    is_independent INTEGER NOT NULL CHECK(is_independent IN (0,1)),
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 1 CHECK(event_count > 0),
    evidence_hash TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    UNIQUE(rule_fingerprint, provider, root_session_hash)
);
CREATE INDEX IF NOT EXISTS idx_rule_observations_support
    ON rule_observations(rule_fingerprint, is_independent, project_scope);
"""

_POSTGRES = """
CREATE TABLE IF NOT EXISTS rule_observations (
    id BIGSERIAL PRIMARY KEY,
    rule_fingerprint TEXT NOT NULL,
    provider TEXT NOT NULL,
    root_session_hash TEXT NOT NULL,
    project_scope TEXT NOT NULL,
    session_kind TEXT NOT NULL CHECK(session_kind IN ('human','mixed','subagent','automation')),
    is_independent INTEGER NOT NULL CHECK(is_independent IN (0,1)),
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 1 CHECK(event_count > 0),
    evidence_hash TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    UNIQUE(rule_fingerprint, provider, root_session_hash)
)
"""


def apply_sqlite(conn: Any) -> None:
    conn.executescript(_SQLITE)
    conn.commit()


def apply_postgres(conn: Any) -> None:
    cursor = conn.cursor()
    cursor.execute(_POSTGRES)
    cursor.execute(
        """CREATE INDEX IF NOT EXISTS idx_rule_observations_support
           ON rule_observations(rule_fingerprint, is_independent, project_scope)"""
    )
    conn.commit()

