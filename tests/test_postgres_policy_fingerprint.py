"""Adversarial runtime checks for the immutable PostgreSQL RLS manifest."""
from __future__ import annotations

import copy

import pytest

from test_postgres_runtime_boundary import (
    COMMAND_POLICIES,
    PERMIT_POLICIES,
    PROTECTED_TABLES,
    TEAM_DENY_TABLES,
    TENANT_TABLES,
    CatalogState,
    _attach_driver,
    _policy_manifest_comment,
    _team_store,
)


def _restamp(state: CatalogState) -> None:
    state.policy_manifest_comment = _policy_manifest_comment(state.policies)


def _assert_connect_rejected(state: CatalogState, match: str = "polic") -> None:
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    with pytest.raises(PermissionError, match=f"(?i){match}"):
        store.connect()

    assert connection.rollback_count == 1
    assert connection.closed is True


def _claims_policy(state: CatalogState, name: str) -> dict[str, object]:
    return next(
        row
        for row in state.policies
        if row["tablename"] == "claims" and row["policyname"] == name
    )


def test_safe_catalog_has_exact_paired_policy_inventory() -> None:
    state = CatalogState()
    expected = {
        (table, policy)
        for table in TENANT_TABLES
        for policy in (*COMMAND_POLICIES.values(), *PERMIT_POLICIES.values())
    }
    expected.update(
        (table, "memorymaster_team_deny")
        for table in set(PROTECTED_TABLES) - set(TENANT_TABLES)
    )

    actual = {
        (str(row["tablename"]), str(row["policyname"]))
        for row in state.policies
    }

    assert actual == expected
    assert len(actual) == 64
    assert not any(
        str(row.get("qual", "")).strip("() ").upper() == "TRUE"
        or str(row.get("with_check", "")).strip("() ").upper() == "TRUE"
        for row in state.policies
    )


@pytest.mark.parametrize(
    "policy",
    [
        PERMIT_POLICIES["SELECT"],
        PERMIT_POLICIES["INSERT"],
        COMMAND_POLICIES["SELECT"],
        COMMAND_POLICIES["UPDATE"],
    ],
)
def test_connect_rejects_missing_policy_even_with_matching_recomputed_stamp(
    policy: str,
) -> None:
    state = CatalogState()
    state.policies = [
        row
        for row in state.policies
        if not (row["tablename"] == "claims" and row["policyname"] == policy)
    ]
    _restamp(state)

    _assert_connect_rejected(state)


@pytest.mark.parametrize("restrictive", [False, True])
def test_connect_rejects_extra_policy_even_with_matching_recomputed_stamp(
    restrictive: bool,
) -> None:
    state = CatalogState()
    extra = copy.deepcopy(_claims_policy(state, COMMAND_POLICIES["SELECT"]))
    extra["policyname"] = (
        "memorymaster_extra_restrict" if restrictive else "memorymaster_extra_permit"
    )
    extra["permissive"] = "RESTRICTIVE" if restrictive else "PERMISSIVE"
    extra["polpermissive"] = not restrictive
    state.policies.append(extra)
    _restamp(state)

    _assert_connect_rejected(state)


def test_connect_rejects_expression_drift_against_stale_migration_stamp() -> None:
    state = CatalogState()
    policy = _claims_policy(state, COMMAND_POLICIES["SELECT"])
    policy["qual"] = f"({policy['qual']}) OR TRUE"

    _assert_connect_rejected(state, "manifest|fingerprint|digest|polic")


@pytest.mark.parametrize(
    "comment",
    [
        None,
        "",
        "copied-from-another-cluster",
        "memorymaster.rls/v1;manifest=0011;sha256=xyz",
        "memorymaster.rls/v1;manifest=0011;sha256=" + ("0" * 64),
        "memorymaster.rls/v1;manifest=0010;sha256=" + ("0" * 64),
    ],
)
def test_connect_rejects_missing_malformed_or_forged_manifest_comment(
    comment: str | None,
) -> None:
    state = CatalogState()
    state.policy_manifest_comment = comment

    _assert_connect_rejected(state, "manifest|fingerprint|digest|polic")


@pytest.mark.parametrize(
    ("policy_name", "field", "unsafe_value"),
    [
        (COMMAND_POLICIES["SELECT"], "qual", "TRUE OR tenant_id = tenant_id"),
        (PERMIT_POLICIES["SELECT"], "roles", ["memorymaster_admin"]),
        (COMMAND_POLICIES["UPDATE"], "cmd", "ALL"),
        (PERMIT_POLICIES["INSERT"], "qual", "tenant_id = tenant_id"),
        (COMMAND_POLICIES["INSERT"], "qual", "tenant_id = tenant_id"),
        (PERMIT_POLICIES["SELECT"], "with_check", "tenant_id = tenant_id"),
        (COMMAND_POLICIES["DELETE"], "with_check", "tenant_id = tenant_id"),
    ],
)
def test_connect_rejects_self_consistent_but_noncanonical_policy_shape(
    policy_name: str,
    field: str,
    unsafe_value: object,
) -> None:
    state = CatalogState()
    policy = _claims_policy(state, policy_name)
    policy[field] = unsafe_value
    _restamp(state)

    _assert_connect_rejected(state)


def test_connect_rejects_permit_and_restrict_predicate_mismatch_when_restamped() -> None:
    state = CatalogState()
    permit = _claims_policy(state, PERMIT_POLICIES["SELECT"])
    permit["qual"] = str(permit["qual"]).replace(
        "claims.visibility = 'private'",
        "claims.visibility <> 'private'",
    )
    _restamp(state)

    _assert_connect_rejected(state)


def test_connect_rejects_schema_version_checksum_drift() -> None:
    state = CatalogState(schema_v0011_checksum="0" * 64)

    _assert_connect_rejected(state, "migration|checksum|schema|version")


@pytest.mark.parametrize("table", TEAM_DENY_TABLES)
@pytest.mark.parametrize("privilege", ["insert", "update", "delete"])
def test_connect_rejects_dml_grants_on_team_deny_tables(
    table: str,
    privilege: str,
) -> None:
    state = CatalogState()
    state.tables[table][f"can_{privilege}"] = True
    state.tables[table][f"has_{privilege}"] = True

    _assert_connect_rejected(state, f"{table}|{privilege}|deny")


@pytest.mark.parametrize(
    ("table", "privilege"),
    [
        (table, privilege)
        for table in ("cache_meta", "schema_versions")
        for privilege in (
            "insert",
            "update",
            "delete",
            "truncate",
            "references",
            "trigger",
        )
    ],
)
def test_connect_rejects_metadata_table_mutation_privileges_before_binding(
    table: str,
    privilege: str,
) -> None:
    state = CatalogState()
    state.metadata_tables[table][f"can_{privilege}"] = True
    state.metadata_tables[table][f"has_{privilege}"] = True
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    with pytest.raises(PermissionError, match=f"(?i)({table}|{privilege})"):
        store.connect()

    assert not any(
        "set_config" in sql.lower()
        for sql, _ in connection.cursor_instance.executed
    )


@pytest.mark.parametrize("table", ["cache_meta", "schema_versions"])
def test_connect_requires_select_only_metadata_access(table: str) -> None:
    state = CatalogState()
    state.metadata_tables[table]["can_select"] = False
    state.metadata_tables[table]["has_select"] = False
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    with pytest.raises(PermissionError, match=f"(?i)({table}|select)"):
        store.connect()

    assert not any(
        "set_config" in sql.lower()
        for sql, _ in connection.cursor_instance.executed
    )


@pytest.mark.parametrize(
    ("case", "field"),
    [
        ("missing", None),
        ("not_unique", "indisunique"),
        ("invalid", "indisvalid"),
        ("not_ready", "indisready"),
        ("wrong_columns", "indexdef"),
        ("extra_column", "indexdef"),
        ("wrong_column_order", "indexdef"),
        ("wrong_tenant_expression", "indexdef"),
        ("wrong_predicate", "predicate"),
        ("tautological_predicate", "predicate"),
    ],
)
def test_connect_requires_exact_tenant_confirmed_tuple_unique_index_before_binding(
    case: str,
    field: str | None,
) -> None:
    state = CatalogState()
    index = next(
        row
        for row in state.claim_identity_indexes
        if row["index_name"] == "idx_claims_public_confirmed_tuple_unique"
    )
    if case == "missing":
        state.claim_identity_indexes.remove(index)
    elif case == "wrong_columns":
        index[field] = str(index[field]).replace("tenant_id", "source_agent")
    elif case == "extra_column":
        index[field] = str(index[field]).replace(
            "predicate, scope)", "predicate, scope, source_agent)"
        )
    elif case == "wrong_column_order":
        index[field] = str(index[field]).replace(
            "subject, predicate", "predicate, subject"
        )
    elif case == "wrong_tenant_expression":
        index[field] = str(index[field]).replace(
            "COALESCE(tenant_id, ''::text)", "tenant_id"
        )
    elif case == "wrong_predicate":
        index[field] = "status = 'candidate'"
    elif case == "tautological_predicate":
        index[field] = f"({index[field]}) OR TRUE"
    else:
        index[field] = False
        alias = {
            "indisunique": "is_unique",
            "indisvalid": "is_valid",
            "indisready": "is_ready",
        }[field]
        index[alias] = False
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    with pytest.raises(
        PermissionError,
        match="(?i)(confirmed|tuple|unique|index|tenant)",
    ):
        store.connect()

    assert not any(
        "set_config" in sql.lower()
        for sql, _ in connection.cursor_instance.executed
    )
