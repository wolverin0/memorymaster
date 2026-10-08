"""A generation counter that only moves when recall-token statistics can change.

``corpus_generation`` (migration 4) advances on any retrieval-relevant claim write,
including confidence, tier, updated_at and last_validated_at. The prompt-recall
tokenizer cached its corpus statistics on it, so every steward or validator write
forced a full rescan: measured 2026-10-04 on 45k claims, warm recall 69 ms, the
first call after one write 681 ms. Those statistics depend only on claim text, on
which claims are live (status) and on entity aliases; ``text_generation`` advances
exactly on those changes. ``corpus_generation`` and the query cache are unchanged.
"""

from __future__ import annotations

from typing import Any

VERSION = 28
DESCRIPTION = "text_generation counter for recall-token statistics"
# Adds a counter row and triggers only: claim rows stay valid on either side, so a
# v27 database and a v28 one may still exchange deltas (bridges/db_merge.py).
MERGE_COMPATIBLE = True

_SQLITE = """
INSERT OR IGNORE INTO cache_meta(key, value) VALUES ('text_generation', 0);
CREATE TRIGGER IF NOT EXISTS claims_textgen_ai AFTER INSERT ON claims BEGIN
    UPDATE cache_meta SET value = value + 1 WHERE key = 'text_generation';
END;
CREATE TRIGGER IF NOT EXISTS claims_textgen_ad AFTER DELETE ON claims BEGIN
    UPDATE cache_meta SET value = value + 1 WHERE key = 'text_generation';
END;
CREATE TRIGGER IF NOT EXISTS claims_textgen_au AFTER UPDATE OF text, status ON claims
WHEN OLD.text IS NOT NEW.text OR OLD.status IS NOT NEW.status BEGIN
    UPDATE cache_meta SET value = value + 1 WHERE key = 'text_generation';
END;
"""

_SQLITE_ALIASES = """
CREATE TRIGGER IF NOT EXISTS entity_aliases_textgen_ai AFTER INSERT ON entity_aliases BEGIN
    UPDATE cache_meta SET value = value + 1 WHERE key = 'text_generation';
END;
CREATE TRIGGER IF NOT EXISTS entity_aliases_textgen_ad AFTER DELETE ON entity_aliases BEGIN
    UPDATE cache_meta SET value = value + 1 WHERE key = 'text_generation';
END;
CREATE TRIGGER IF NOT EXISTS entity_aliases_textgen_au AFTER UPDATE OF alias ON entity_aliases BEGIN
    UPDATE cache_meta SET value = value + 1 WHERE key = 'text_generation';
END;
"""


def _has(conn: Any, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def apply_sqlite(conn: Any) -> None:
    if not _has(conn, "cache_meta") or not _has(conn, "claims"):
        return  # bare migration tests; migration 4 creates both in a real database
    conn.executescript(_SQLITE)
    if _has(conn, "entity_aliases"):
        conn.executescript(_SQLITE_ALIASES)
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def apply_postgres(conn: Any) -> None:
    """Fail closed: the recall tokenizer reads SQLite only, like migrations 21 and 26-27."""
    del conn
    raise RuntimeError("migration 28 is SQLite-only; the recall tokenizer reads SQLite only")
