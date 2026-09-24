"""Track which governed claims the compiled profile has already mapped.

Review F-03 follow-up: the claims evidence stream first used a MAX(id)
watermark over the claims eligible at run time. A claim can become eligible
long after higher ids were compiled (the steward confirming an agent
candidate, S1 re-confirming a stale claim, a conflict resolved to confirmed),
and Dreaming candidates are eligible the moment they exist, so the watermark
skipped those claims permanently and silently. A claims run now selects the
eligible claims that are not in this table, and records each claim it hands to
the mapper here in the same transaction as the candidates it produced.

Only claims actually delivered to the mapper are recorded; an eligible claim
that is not usable yet (not temporally current) stays pending. Deleting a run
releases its claims for mapping again.
"""

from __future__ import annotations

from typing import Any


VERSION = 27
DESCRIPTION = "Track governed claims already mapped by the compiled profile"


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS compiled_profile_claim_seen (
    claim_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES compiled_profile_runs(id) ON DELETE CASCADE,
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compiled_profile_claim_seen_run
    ON compiled_profile_claim_seen(run_id);
"""


def apply_sqlite(conn: Any) -> None:
    conn.executescript(_SQLITE_SCHEMA)
    conn.commit()


def apply_postgres(conn: Any) -> None:
    """Fail closed: the compiled profile is SQLite-only, like migrations 21 and 26."""
    del conn
    raise RuntimeError("migration 27 is SQLite-only; the compiled profile is SQLite-only")
