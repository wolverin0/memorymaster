"""Record which evidence stream a compiled-profile run consumed.

Review F-03: the profile compiler read only ``verbatim_memories``, and verbatim
capture has been opt-in since 2026-07-13, so the profile froze at 2026-08-24
and SessionStart kept injecting it as current. When no new verbatim input
exists, the engine now compiles from governed claims (confirmed claims and
Dreaming-applied claims) instead. Each stream keeps its own watermarks, so a
run must say which one it advances: ``source`` is ``verbatim`` (every run
before this migration) or ``claims``.

Claim supports reuse ``compiled_profile_supports`` with the claim id negated
in ``verbatim_id`` (the primary key stays ``(fact_id, verbatim_id)`` and the
exact-support manifest and mismatch check keep working unchanged).
"""

from __future__ import annotations

from typing import Any


VERSION = 26
DESCRIPTION = "Record the evidence source of compiled profile runs"


def _columns(conn: Any) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(compiled_profile_runs)")}


def apply_sqlite(conn: Any) -> None:
    columns = _columns(conn)
    if columns and "source" not in columns:
        conn.execute(
            "ALTER TABLE compiled_profile_runs ADD COLUMN source TEXT NOT NULL "
            "DEFAULT 'verbatim' CHECK (source IN ('verbatim','claims'))"
        )
    conn.commit()


def apply_postgres(conn: Any) -> None:
    """Fail closed: the compiled profile is SQLite-only, like migration 21."""
    del conn
    raise RuntimeError("migration 26 is SQLite-only; the compiled profile is SQLite-only")
