"""Add SQLite persistence for the resumable compiled user profile."""

from __future__ import annotations

from typing import Any


VERSION = 21
DESCRIPTION = "Add compiled user profile runs, candidates, facts, and supports"


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS compiled_profile_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK (status IN (
        'mapping','reducing','completed','failed','cancelled'
    )),
    active_slot INTEGER CHECK (active_slot IS NULL OR active_slot = 1),
    start_watermark INTEGER NOT NULL DEFAULT 0,
    current_watermark INTEGER NOT NULL DEFAULT 0,
    target_watermark INTEGER NOT NULL DEFAULT 0,
    map_model TEXT NOT NULL,
    reduce_model TEXT NOT NULL,
    map_calls INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    output_hash TEXT CHECK (output_hash IS NULL OR length(output_hash) = 64),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_compiled_profile_active_run
    ON compiled_profile_runs(active_slot) WHERE active_slot IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_compiled_profile_runs_status
    ON compiled_profile_runs(status, updated_at);

CREATE TABLE IF NOT EXISTS compiled_profile_candidates (
    run_id INTEGER NOT NULL REFERENCES compiled_profile_runs(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    category TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 240),
    volatility TEXT NOT NULL CHECK (volatility IN ('stable','preference')),
    support_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS compiled_profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_key TEXT NOT NULL UNIQUE CHECK (length(fact_key) = 64),
    category TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 240),
    volatility TEXT NOT NULL CHECK (volatility IN ('stable','preference')),
    status TEXT NOT NULL CHECK (status IN ('active','superseded','expired')),
    support_hash TEXT NOT NULL CHECK (length(support_hash) = 64),
    support_count INTEGER NOT NULL DEFAULT 0,
    independent_sessions INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_supported_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    replaced_by_fact_id INTEGER REFERENCES compiled_profile_facts(id)
);
CREATE INDEX IF NOT EXISTS idx_compiled_profile_facts_active
    ON compiled_profile_facts(status, category, predicate);

CREATE TABLE IF NOT EXISTS compiled_profile_supports (
    fact_id INTEGER NOT NULL REFERENCES compiled_profile_facts(id) ON DELETE CASCADE,
    verbatim_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    message_hash TEXT NOT NULL CHECK (length(message_hash) = 64),
    supported_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (fact_id, verbatim_id)
);
CREATE INDEX IF NOT EXISTS idx_compiled_profile_support_verbatim
    ON compiled_profile_supports(verbatim_id, fact_id);
"""


def apply_sqlite(conn: Any) -> None:
    conn.executescript(_SQLITE_SCHEMA)
    conn.commit()


def apply_postgres(conn: Any) -> None:
    del conn
    raise RuntimeError("migration 21 is SQLite-only; PostgreSQL rollout is deferred")
