"""Record WHAT a graph-observation job concluded, not merely that it ran.

``status='completed'`` collapsed two opposite outcomes: "synthesized a
component" and "found nothing to synthesize". Production carried 3,146
completed discovery jobs against 2 observations ever, and the only trace of
*why* was a sha256 of the diagnostic codes — the reason was destroyed at write
time. This adds an explicit terminal ``outcome`` plus the diagnostic codes as
readable text, leaving ``status`` untouched so existing consumers keep working.
"""

from __future__ import annotations

from typing import Any


VERSION = 22
DESCRIPTION = "Record graph observation job outcome and readable diagnostic codes"

_JOB_OUTCOMES = (
    "components_found",
    "no_supports",
    "no_components",
    "observation_emitted",
    "no_signal",
)

_OUTCOME_CHECK = " OR ".join(f"outcome = '{value}'" for value in _JOB_OUTCOMES)


def _columns(conn: Any) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(graph_observation_jobs)")}


def apply_sqlite(conn: Any) -> None:
    columns = _columns(conn)
    if "outcome" not in columns:
        conn.execute(
            "ALTER TABLE graph_observation_jobs ADD COLUMN outcome TEXT "
            f"CHECK (outcome IS NULL OR {_OUTCOME_CHECK})"
        )
    if "diagnostic_codes" not in columns:
        conn.execute(
            "ALTER TABLE graph_observation_jobs ADD COLUMN diagnostic_codes TEXT"
        )
    # Rows completed before this migration stay NULL on purpose: we do not know
    # what they concluded, and inventing an outcome would repeat the original
    # defect in the other direction.
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_graph_observation_jobs_outcome
           ON graph_observation_jobs(stage, outcome)"""
    )
    conn.commit()


def apply_postgres(conn: Any) -> None:
    """Fail closed because PPR-7 PostgreSQL rollout is explicitly deferred."""
    raise RuntimeError("migration 22 is SQLite-only; PostgreSQL rollout is deferred")
