"""Regression tests for the auto-ingest stop hook (#128).

The hook at ``config_templates/hooks/memorymaster-auto-ingest.py`` inserts
claims via raw SQL. A 2026-04-22 audit found it never inserted the companion
``citations`` row, so every hook-born claim failed the steward
``min_citations >= 1`` gate and stayed unpromotable forever (~93% of live
candidates).

These tests lock in two invariants:

1.  **Pattern test** — the exact SQL pair the hook executes must leave every
    claim with at least one citation.
2.  **Source guard** — the template file must still contain an
    ``INSERT INTO citations`` inside its claims-insert path, as a cheap
    text-level regression tripwire against future refactors that drop it.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-auto-ingest.py"


# Minimal schema that mirrors the columns the hook writes to. Kept inline so
# the test does not depend on MemoryService's full schema init, which evolves
# independently and would make this test fragile.
_CLAIMS_DDL = """
CREATE TABLE claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    normalized_text TEXT,
    claim_type TEXT,
    subject TEXT,
    predicate TEXT,
    scope TEXT,
    status TEXT,
    confidence REAL,
    source_agent TEXT,
    created_at TEXT,
    updated_at TEXT,
    tier TEXT,
    version INTEGER,
    visibility TEXT
)
"""

_CITATIONS_DDL = """
CREATE TABLE citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    locator TEXT,
    excerpt TEXT,
    created_at TEXT NOT NULL
)
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute(_CLAIMS_DDL)
    c.execute(_CITATIONS_DDL)
    yield c
    c.close()


def _hook_ingest(conn: sqlite3.Connection, claim: dict, scope: str) -> int:
    """Mirror of the fixed auto-ingest hook's per-claim DB logic.

    Kept in-test (rather than importing the template) because the template
    uses an unsubstituted ``__MEMORYMASTER_PROJECT_ROOT__`` placeholder and
    cannot be imported as a module without setup-hooks processing it first.
    """
    now = datetime.now(timezone.utc).isoformat()
    idem = f"llm-stop-{abs(hash(claim['text'])) & 0xFFFFFFFF}"
    cur = conn.execute(
        """INSERT INTO claims (text, idempotency_key, normalized_text, claim_type,
           subject, predicate, scope, status, confidence,
           source_agent, created_at, updated_at, tier, version, visibility)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', 0.6,
           'llm-stop-hook', ?, ?, 'working', 1, 'public')""",
        (claim["text"], idem, claim["text"].lower(), claim.get("claim_type", "fact"),
         claim.get("subject", "codebase"), claim.get("predicate", "observation"),
         scope, now, now),
    )
    conn.execute(
        """INSERT INTO citations (claim_id, source, locator, excerpt, created_at)
           VALUES (?, 'llm-stop-hook', ?, ?, ?)""",
        (cur.lastrowid, scope, claim["text"][:200], now),
    )
    return cur.lastrowid


def test_hook_pattern_creates_citation_per_claim(conn):
    claims = [
        {"text": "Recall hook needs skip_qdrant=True on Windows environments"},
        {"text": "Sensitivity filter F1 0.995 on adversarial corpus"},
        {"text": "Scope canonicalization folds Copy/dash/underscore variants"},
    ]
    for c in claims:
        _hook_ingest(conn, c, "project:memorymaster")

    claim_count = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    citation_count = conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0]
    orphan_count = conn.execute(
        "SELECT COUNT(*) FROM claims WHERE id NOT IN (SELECT claim_id FROM citations)"
    ).fetchone()[0]

    assert claim_count == 3
    assert citation_count == 3
    assert orphan_count == 0, "Every hook-born claim must have at least one citation (#128)"


def test_hook_citation_locator_is_scope(conn):
    """The citation's locator should carry the scope so audits can trace origin."""
    _hook_ingest(conn, {"text": "whatever"}, "project:memorymaster")
    row = conn.execute(
        "SELECT source, locator FROM citations WHERE claim_id = (SELECT id FROM claims LIMIT 1)"
    ).fetchone()
    assert row == ("llm-stop-hook", "project:memorymaster")


def test_template_still_contains_citation_insert():
    """Text-level regression guard: the template must keep the citations INSERT.

    Matches the exact invariant broken by the pre-fix version: claims INSERT
    with no citations INSERT. This test will fail loudly if a future refactor
    removes the companion write.
    """
    assert TEMPLATE_PATH.exists(), f"Template missing at {TEMPLATE_PATH}"
    src = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "INSERT INTO claims" in src, "Template no longer inserts into claims — did it move?"
    # Case-insensitive because we don't care about SQL keyword casing.
    assert re.search(r"INSERT\s+INTO\s+citations", src, re.IGNORECASE), (
        "Template inserts into claims but not citations. "
        "This regresses #128: every hook-born claim becomes unpromotable. "
        "See scripts/backfill_stop_hook_citations.py for the forensic story."
    )
