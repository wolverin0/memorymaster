"""Adversarial semantic checks for PostgreSQL RLS policy expressions."""
from __future__ import annotations

import importlib

import pytest

from test_postgres_runtime_boundary import (
    COMMAND_POLICIES,
    PERMIT_POLICIES,
    TENANT_TABLES,
    CatalogState,
    _attach_driver,
    _policy_manifest_comment,
    _team_store,
)


def _migration():
    return importlib.import_module(
        "memorymaster.stores.migrations.0011_postgres_scoped_force_rls"
    )


def _paired_rows(
    state: CatalogState,
    table: str,
    command: str,
) -> list[dict[str, object]]:
    names = {COMMAND_POLICIES[command], PERMIT_POLICIES[command]}
    rows = [
        row
        for row in state.policies
        if row["tablename"] == table and row["policyname"] in names
    ]
    assert {str(row["policyname"]) for row in rows} == names
    return rows


def _set_paired_expression(
    state: CatalogState,
    table: str,
    command: str,
    expression: str,
) -> None:
    for row in _paired_rows(state, table, command):
        row["qual"] = None if command == "INSERT" else expression
        row["with_check"] = expression if command in {"INSERT", "UPDATE"} else None


def _canonical_v0011_state() -> CatalogState:
    state = CatalogState()
    migration = _migration()
    for table in TENANT_TABLES:
        _set_paired_expression(state, table, "SELECT", migration._READ_PREDICATES[table])
        for command in ("INSERT", "UPDATE", "DELETE"):
            _set_paired_expression(
                state,
                table,
                command,
                migration._WRITE_PREDICATES[table],
            )
    state.policy_manifest_comment = _policy_manifest_comment(state.policies)
    return state


def _assert_connect_rejected(state: CatalogState) -> None:
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    with pytest.raises(PermissionError, match="(?i)(policy|expression|predicate|rls)"):
        store.connect()

    assert connection.rollback_count == 1
    assert connection.closed is True


def _drift_paired_expression(
    state: CatalogState,
    table: str,
    command: str,
    safe_fragment: str,
    unsafe_fragment: str,
) -> None:
    field = "with_check" if command == "INSERT" else "qual"
    expression = str(_paired_rows(state, table, command)[0][field])
    assert safe_fragment in expression
    _set_paired_expression(
        state,
        table,
        command,
        expression.replace(safe_fragment, unsafe_fragment),
    )
    state.policy_manifest_comment = _policy_manifest_comment(state.policies)


def test_connect_accepts_canonical_v0011_policy_expression_catalog() -> None:
    state = _canonical_v0011_state()
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    assert store.connect() is connection
    connection.close()


def test_connect_accepts_postgres_deparsed_equivalent_policy_catalog() -> None:
    state = _canonical_v0011_state()
    for row in state.policies:
        for field in ("qual", "with_check"):
            expression = row[field]
            if expression is None or str(expression).upper() == "FALSE":
                continue
            deparsed = str(expression).replace(
                "IN ('public', 'private')",
                "= ANY (ARRAY['public'::text, 'private'::text])",
            )
            deparsed = deparsed.replace("'[]'", "'[]'::text")
            deparsed = deparsed.replace("'array'", "'array'::text")
            row[field] = f"(({deparsed}))"
    state.policy_manifest_comment = _policy_manifest_comment(state.policies)
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    assert store.connect() is connection
    connection.close()


def test_connect_rejects_restamped_paired_true_claims_select_policies() -> None:
    state = _canonical_v0011_state()
    _set_paired_expression(state, "claims", "SELECT", "TRUE")
    state.policy_manifest_comment = _policy_manifest_comment(state.policies)

    _assert_connect_rejected(state)


def test_connect_preserves_literal_case_in_policy_fingerprint() -> None:
    state = _canonical_v0011_state()
    rows = _paired_rows(state, "claims", "SELECT")
    for row in rows:
        row["qual"] = str(row["qual"]).replace("'public'", "'PUBLIC'")
    state.policy_manifest_comment = _policy_manifest_comment(state.policies)

    _assert_connect_rejected(state)


@pytest.mark.parametrize(
    ("table", "command", "safe_fragment", "unsafe_fragment"),
    [
        (
            "claims",
            "SELECT",
            "claims.tenant_id = NULLIF(current_setting('memorymaster.tenant_id', true), '')",
            "claims.tenant_id IS NOT NULL",
        ),
        (
            "claims",
            "SELECT",
            "? claims.scope",
            "? 'project:alpha'",
        ),
        (
            "claims",
            "SELECT",
            "claims.visibility = 'private' AND claims.source_agent = "
            "NULLIF(current_setting('memorymaster.principal', true), '')",
            "claims.visibility = 'private'",
        ),
        (
            "claims",
            "UPDATE",
            "claims.source_agent = "
            "NULLIF(current_setting('memorymaster.principal', true), '')",
            "claims.source_agent IS NOT NULL",
        ),
        (
            "claims",
            "UPDATE",
            "claims.visibility IN ('public', 'private')",
            "claims.visibility IS NOT NULL",
        ),
        (
            "citations",
            "SELECT",
            "mm_claim.visibility = 'private' AND mm_claim.source_agent = "
            "NULLIF(current_setting('memorymaster.principal', true), '')",
            "mm_claim.visibility = 'private'",
        ),
        (
            "claim_embeddings",
            "INSERT",
            "mm_claim.source_agent = "
            "NULLIF(current_setting('memorymaster.principal', true), '')",
            "mm_claim.source_agent IS NOT NULL",
        ),
        (
            "claim_links",
            "SELECT",
            "? mm_target.scope",
            "? 'project:alpha'",
        ),
        (
            "contradiction_verdicts",
            "DELETE",
            "mm_b.source_agent = "
            "NULLIF(current_setting('memorymaster.principal', true), '')",
            "mm_b.source_agent IS NOT NULL",
        ),
        (
            "events",
            "SELECT",
            "mm_claim.visibility = 'private' AND mm_claim.source_agent = "
            "NULLIF(current_setting('memorymaster.principal', true), '')",
            "mm_claim.visibility = 'private'",
        ),
    ],
    ids=(
        "claims-tenant",
        "claims-scope",
        "claims-private-owner",
        "claims-write-owner",
        "claims-write-visibility",
        "citation-private-owner",
        "embedding-write-owner",
        "link-target-scope",
        "verdict-target-owner",
        "event-private-owner",
    ),
)
def test_connect_rejects_restamped_paired_authority_predicate_drift(
    table: str,
    command: str,
    safe_fragment: str,
    unsafe_fragment: str,
) -> None:
    state = _canonical_v0011_state()
    _drift_paired_expression(
        state,
        table,
        command,
        safe_fragment,
        unsafe_fragment,
    )

    _assert_connect_rejected(state)
