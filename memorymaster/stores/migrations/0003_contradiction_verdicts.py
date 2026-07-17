"""0003_contradiction_verdicts — LLM verdict cache for the contradiction probe.

The suspected-contradictions probe (:mod:`memorymaster.contradiction_probe`)
asks an LLM whether two topically-similar claims contradict. Judging is the
expensive step, so verdicts are cached keyed on the (canonical-ordered) claim
pair + model + prompt_version: re-running the probe never re-pays for a pair
already judged by the same model/prompt. A prompt_version bump invalidates the
cache for that pair automatically (new key).
"""
from __future__ import annotations

VERSION = 3
DESCRIPTION = "contradiction_verdicts cache for the suspected-contradictions probe"

_DDL = """
CREATE TABLE IF NOT EXISTS contradiction_verdicts (
    claim_a_id INTEGER NOT NULL,
    claim_b_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    contradicts INTEGER NOT NULL,
    severity TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (claim_a_id, claim_b_id, model, prompt_version)
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
