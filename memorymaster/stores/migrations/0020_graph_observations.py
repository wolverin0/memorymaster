"""Add SQLite persistence for governed graph observations."""

from __future__ import annotations

from typing import Any


VERSION = 20
DESCRIPTION = "Add governed graph observations and leased jobs"


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_observations (
    observation_claim_id INTEGER PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE,
    observation_type TEXT NOT NULL CHECK (observation_type IN (
        'decision','commitment','constraint','dependency','state_change',
        'recurring_pattern','stable_relationship','root_cause'
    )),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    scope TEXT NOT NULL,
    tenant_id TEXT,
    support_hash TEXT NOT NULL CHECK (length(support_hash) = 64),
    algorithm_version TEXT NOT NULL,
    ontology_version TEXT NOT NULL,
    evidence_window_start TEXT,
    evidence_window_end TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_observations_replay
    ON graph_observations(
        COALESCE(tenant_id, ''), scope, support_hash,
        algorithm_version, ontology_version
    );
CREATE INDEX IF NOT EXISTS idx_graph_observations_scope
    ON graph_observations(COALESCE(tenant_id, ''), scope, observation_type);

CREATE TABLE IF NOT EXISTS graph_observation_supports (
    observation_claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    supporting_claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE RESTRICT,
    evidence_item_id INTEGER NOT NULL REFERENCES evidence_items(id) ON DELETE RESTRICT,
    source_item_id INTEGER NOT NULL REFERENCES source_items(id) ON DELETE RESTRICT,
    source_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
    target_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
    relation TEXT NOT NULL,
    ontology_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (
        observation_claim_id, supporting_claim_id, evidence_item_id,
        source_entity_id, target_entity_id, relation, ontology_version
    )
);
CREATE INDEX IF NOT EXISTS idx_graph_observation_support_claim
    ON graph_observation_supports(supporting_claim_id, observation_claim_id);
CREATE INDEX IF NOT EXISTS idx_graph_observation_support_evidence
    ON graph_observation_supports(evidence_item_id, observation_claim_id);
CREATE INDEX IF NOT EXISTS idx_graph_observation_support_source
    ON graph_observation_supports(source_item_id, observation_claim_id);

CREATE TABLE IF NOT EXISTS graph_observation_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT,
    scope TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('discover','synthesize')),
    status TEXT NOT NULL CHECK (status IN (
        'pending','leased','retryable','blocked','completed','cancelled'
    )),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    support_hash TEXT CHECK (support_hash IS NULL OR length(support_hash) = 64),
    algorithm_version TEXT NOT NULL,
    ontology_version TEXT NOT NULL,
    support_manifest_json TEXT NOT NULL DEFAULT '[]',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    error_code TEXT,
    diagnostic_hash TEXT CHECK (diagnostic_hash IS NULL OR length(diagnostic_hash) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_observation_jobs_replay
    ON graph_observation_jobs(
        COALESCE(tenant_id, ''), scope, stage, content_hash,
        algorithm_version, ontology_version
    );
CREATE INDEX IF NOT EXISTS idx_graph_observation_jobs_due
    ON graph_observation_jobs(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_graph_observation_jobs_lease
    ON graph_observation_jobs(status, lease_expires_at);
"""


def apply_sqlite(conn: Any) -> None:
    conn.executescript(_SQLITE_SCHEMA)
    conn.commit()


def apply_postgres(conn: Any) -> None:
    """Fail closed because PPR-7 PostgreSQL rollout is explicitly deferred."""
    raise RuntimeError("migration 20 is SQLite-only; PostgreSQL rollout is deferred")
