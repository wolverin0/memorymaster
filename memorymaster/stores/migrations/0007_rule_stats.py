"""0007_rule_stats — per-fingerprint correction tally for rule confidence bootstrap (v3.28).

The rule-miner (:mod:`memorymaster.knowledge.rule_miner`) historically ingested every
mined rule at a flat ``confidence=0.4``. A correction the user makes repeatedly
is more load-bearing than a one-off, so v3.28 bootstraps confidence by how many
times the SAME correction (identified by a stable ``rule_fingerprint`` over the
trigger+action) has been mined::

    confidence = 0.4 + 0.3 * min(correction_count / 3, 1.0)
    # count 1/2/3+ = 0.50 / 0.60 / 0.70

That tally is a small per-fingerprint counter — not a per-row fact and not a
column on any large table — so it lives in its own ``rule_stats`` table keyed by
fingerprint, mirroring the ``miner_state`` KV pattern from migration 0002.

``rule_fingerprint`` is the PRIMARY KEY (``TEXT``); ``correction_count`` defaults
to 1 (a row exists only once at least one mining event happened). ``last_mined``
is an ISO-8601 timestamp; ``confidence_at_last_mine`` records the bootstrapped
confidence emitted on the most recent mine for observability.

Idempotent (``CREATE TABLE IF NOT EXISTS``) and re-entrant, so the runner may
re-apply it and the miner's own guard may self-create the table out-of-band.
Postgres support is included for SQLite<->Postgres parity even though rule
mining reads verbatim (SQLite-only) today — the claims store, where bootstrapped
confidence ultimately lands, runs on both backends.
"""
from __future__ import annotations

VERSION = 7
DESCRIPTION = "rule_stats table - per-fingerprint correction tally for confidence bootstrap"

_DDL = """
CREATE TABLE IF NOT EXISTS rule_stats (
    rule_fingerprint TEXT PRIMARY KEY,
    correction_count INTEGER NOT NULL DEFAULT 1,
    last_mined TEXT NOT NULL,
    confidence_at_last_mine REAL
)
""".strip()


def apply_sqlite(conn) -> None:
    conn.execute(_DDL)
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def apply_postgres(conn) -> None:
    cur = conn.cursor()
    cur.execute(_DDL)
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()
