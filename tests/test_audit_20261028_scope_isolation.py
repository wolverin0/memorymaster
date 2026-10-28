"""Scope isolation of the Auto Dream export from AUDIT-2026-10-28 (risk R6).

`dream_seed` derives `project:<folder>` from the project path and filters
exportable claims with `scope LIKE 'project:<folder>%'`. A prefix match lets a
sibling project whose name starts with the same text (or matches a `_`
wildcard) leak into this project's Claude memory directory.
"""

from __future__ import annotations

import sqlite3

import pytest

from memorymaster.bridges.dream_bridge import _query_exportable_claims


def _conn_with_scopes(*scopes: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT, scope TEXT, status TEXT, "
        "tier TEXT, quality_score REAL, confidence REAL, access_count INTEGER)"
    )
    conn.execute("CREATE TABLE citations (claim_id INTEGER, source TEXT, locator TEXT, excerpt TEXT)")
    for index, scope in enumerate(scopes, start=1):
        conn.execute(
            "INSERT INTO claims VALUES (?, ?, ?, 'confirmed', 'core', 0.9, 0.9, 0)",
            (index, f"fact for {scope}", scope),
        )
    return conn


def test_export_includes_exact_project_and_excludes_unrelated():
    conn = _conn_with_scopes("project:app", "project:other")
    scopes = {c["scope"] for c in _query_exportable_claims(conn, scope_filter="project:app")}
    assert scopes == {"project:app"}


@pytest.mark.skip(
    reason=(
        "AUDIT-2026-10-28 R6: dream_bridge._query_exportable_claims filters with "
        "an unescaped `scope LIKE '<scope>%'`, so project:app exports "
        "project:apple / project:app-secrets and project:my_app matches project:myXapp."
    )
)
@pytest.mark.parametrize(
    ("wanted", "foreign"),
    [("project:app", "project:apple"), ("project:app", "project:app-secrets"), ("project:my_app", "project:myXapp")],
)
def test_export_does_not_leak_prefix_sibling_projects(wanted, foreign):
    conn = _conn_with_scopes(wanted, foreign)
    scopes = {c["scope"] for c in _query_exportable_claims(conn, scope_filter=wanted)}
    assert foreign not in scopes
