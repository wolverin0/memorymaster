"""0001_initial — baseline marker for the v3.20.0 migration framework.

This migration is intentionally a no-op. As of v3.20.0-S1 the pre-existing
schema (from ``schema.sql`` / ``schema_postgres.sql`` and the inline
``_ensure_*_schema`` helpers in storage layers) is treated as the baseline
v0001. The runner stamps this version into ``schema_versions`` on first
run so subsequent NEW migrations (v0002+) apply cleanly on top.

Future migrations starting at v0002 will contain real ``ALTER TABLE`` /
``CREATE INDEX`` / data-backfill DDL.
"""
from __future__ import annotations

VERSION = 1
DESCRIPTION = "baseline (existing schema as of v3.20.0)"


def apply_sqlite(conn) -> None:  # noqa: ARG001 — intentional no-op baseline
    """No-op: existing schema is the baseline."""
    return None


def apply_postgres(conn) -> None:  # noqa: ARG001 — intentional no-op baseline
    """No-op: existing schema is the baseline."""
    return None
