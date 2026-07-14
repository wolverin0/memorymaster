"""Opt-in integration proof for the PostgreSQL team-runtime RLS boundary.

This module never falls back to ``DATABASE_URL`` or another application DSN.
It runs only when both purpose-specific DSNs are present and the operator also
sets ``MEMORYMASTER_TEST_POSTGRES_RLS_DISPOSABLE=1``.  The admin DSN owns schema
initialization/migrations; the app DSN must be a distinct, non-owner role with
ordinary DML/sequence privileges but no BYPASSRLS, schema CREATE, TRUNCATE, or
REFERENCES/TRIGGER privilege.  The migrator must be SUPERUSER or BYPASSRLS;
the application role receives read-only access to governance metadata.

Events are append-only, so the fixture uses UUID-namespaced tenants, scopes,
claims, and idempotency keys rather than destructive global cleanup.  Run this
only against a database whose complete lifecycle is disposable.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

import pytest

from memorymaster.core.models import CitationInput, Claim, ClaimLink
from memorymaster.stores.migrations import discover_migrations
from memorymaster.stores.postgres_store import (
    POSTGRES_COMMAND_POLICIES,
    POSTGRES_PERMIT_POLICIES,
    POSTGRES_PROTECTED_TABLES,
    POSTGRES_TEAM_DENY_TABLES,
    POSTGRES_TENANT_POLICY_TABLES,
    PostgresStore,
)


ADMIN_DSN_ENV = "MEMORYMASTER_TEST_POSTGRES_DSN"
APP_DSN_ENV = "MEMORYMASTER_TEST_POSTGRES_APP_DSN"
DISPOSABLE_OPT_IN_ENV = "MEMORYMASTER_TEST_POSTGRES_RLS_DISPOSABLE"
_LIVE_DSN_ENVS = ("DATABASE_URL", "POSTGRES_DSN", "MEMORYMASTER_POSTGRES_DSN")
_BLOCKED_REASON = (
    "BLOCKED-EXTERNAL: real PostgreSQL RLS verification requires both "
    f"{ADMIN_DSN_ENV} and {APP_DSN_ENV}, plus {DISPOSABLE_OPT_IN_ENV}=1"
)

pytestmark = pytest.mark.postgres


@dataclass(frozen=True)
class PgConfig:
    admin_dsn: str = field(repr=False)
    app_dsn: str = field(repr=False)
    run_id: str
    tenant_a: str
    tenant_b: str
    scope_a: str
    scope_b: str
    alice: str
    bob: str


@dataclass(frozen=True)
class PreparedDatabase:
    config: PgConfig
    admin_role: str
    app_role: str
    applied_versions: frozenset[int]


@dataclass(frozen=True)
class DatabaseIdentity:
    database: str
    role: str
    superuser: bool
    bypass_rls: bool
    replication: bool
    create_role: bool
    create_db: bool


@dataclass(frozen=True)
class RuntimeStores:
    alice_a_scope_a: PostgresStore
    bob_a_scope_a: PostgresStore
    alice_a_scope_b: PostgresStore
    alice_b_scope_a: PostgresStore


@dataclass(frozen=True)
class SeedRows:
    public_a: Claim
    private_a: Claim
    private_a_target: Claim
    scope_b: Claim
    tenant_b: Claim
    private_link: ClaimLink


def _same_secret(left: str, right: str) -> bool:
    return bool(left and right) and secrets.compare_digest(left, right)


@pytest.fixture(scope="module")
def pg_config() -> PgConfig:
    admin_dsn = os.getenv(ADMIN_DSN_ENV, "").strip()
    app_dsn = os.getenv(APP_DSN_ENV, "").strip()
    opted_in = os.getenv(DISPOSABLE_OPT_IN_ENV, "").strip() == "1"
    if not admin_dsn or not app_dsn or not opted_in:
        pytest.skip(_BLOCKED_REASON)
    if _same_secret(admin_dsn, app_dsn):
        pytest.fail("PostgreSQL RLS integration requires distinct admin and app DSNs.")
    for env_name in _LIVE_DSN_ENVS:
        live_dsn = os.getenv(env_name, "").strip()
        if _same_secret(admin_dsn, live_dsn) or _same_secret(app_dsn, live_dsn):
            pytest.fail(f"Refusing to reuse {env_name} for disposable RLS tests.")

    run_id = uuid4().hex
    return PgConfig(
        admin_dsn=admin_dsn,
        app_dsn=app_dsn,
        run_id=run_id,
        tenant_a=f"rls-{run_id}-tenant-a",
        tenant_b=f"rls-{run_id}-tenant-b",
        scope_a=f"project:rls-{run_id}-a",
        scope_b=f"project:rls-{run_id}-b",
        alice=f"rls-{run_id}-alice",
        bob=f"rls-{run_id}-bob",
    )


def _database_identity(psycopg: Any, dsn: str) -> DatabaseIdentity:
    with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_database(), current_user, rolsuper, rolbypassrls,
                   rolreplication, rolcreaterole, rolcreatedb
            FROM pg_roles WHERE rolname = current_user
            """
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL connection identity could not be verified.")
    return DatabaseIdentity(
        str(row[0]),
        str(row[1]),
        bool(row[2]),
        bool(row[3]),
        bool(row[4]),
        bool(row[5]),
        bool(row[6]),
    )


@pytest.fixture(scope="module")
def prepared_database(pg_config: PgConfig) -> PreparedDatabase:
    try:
        import psycopg
    except ImportError:
        pytest.skip("BLOCKED-EXTERNAL: psycopg is required for PostgreSQL RLS tests")

    try:
        admin_identity = _database_identity(psycopg, pg_config.admin_dsn)
        app_identity = _database_identity(psycopg, pg_config.app_dsn)
    except psycopg.OperationalError:
        pytest.skip("BLOCKED-EXTERNAL: configured PostgreSQL test DSNs are unreachable")
    if admin_identity.database != app_identity.database:
        pytest.fail("Admin and app DSNs must target the same disposable database.")
    if admin_identity.role == app_identity.role:
        pytest.fail("Admin and app DSNs must authenticate as distinct roles.")
    if not admin_identity.superuser and not admin_identity.bypass_rls:
        pytest.fail("The PostgreSQL migrator must be SUPERUSER or BYPASSRLS.")
    if any(
        (
            app_identity.superuser,
            app_identity.bypass_rls,
            app_identity.replication,
            app_identity.create_role,
            app_identity.create_db,
        )
    ):
        pytest.fail("The PostgreSQL app role has a forbidden role attribute.")

    admin_store = PostgresStore(pg_config.admin_dsn)
    admin_store.init_db()
    from psycopg import sql

    with psycopg.connect(pg_config.admin_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE public.events TO {}").format(
                sql.Identifier(app_identity.role)
            )
        )
        cur.execute(
            sql.SQL("REVOKE UPDATE, DELETE ON TABLE public.events FROM {}").format(
                sql.Identifier(app_identity.role)
            )
        )
        cur.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION "
                "public.memorymaster_event_chain_head() TO {}"
            ).format(sql.Identifier(app_identity.role))
        )
    with psycopg.connect(pg_config.admin_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_versions ORDER BY version")
        applied = frozenset(int(row[0]) for row in cur.fetchall())
    expected = {migration.version for migration in discover_migrations()}
    if not expected.issubset(applied):
        pytest.fail("Admin initialization did not apply every discovered migration.")
    return PreparedDatabase(pg_config, admin_identity.role, app_identity.role, applied)


def _runtime_store(
    config: PgConfig,
    *,
    tenant_id: str,
    principal: str,
    scope: str,
) -> PostgresStore:
    return PostgresStore(
        config.app_dsn,
        tenant_id=tenant_id,
        require_tenant=True,
        principal=principal,
        allowed_scopes=(scope,),
    )


@pytest.fixture(scope="module")
def runtime_stores(prepared_database: PreparedDatabase) -> RuntimeStores:
    config = prepared_database.config
    return RuntimeStores(
        alice_a_scope_a=_runtime_store(
            config,
            tenant_id=config.tenant_a,
            principal=config.alice,
            scope=config.scope_a,
        ),
        bob_a_scope_a=_runtime_store(
            config,
            tenant_id=config.tenant_a,
            principal=config.bob,
            scope=config.scope_a,
        ),
        alice_a_scope_b=_runtime_store(
            config,
            tenant_id=config.tenant_a,
            principal=config.alice,
            scope=config.scope_b,
        ),
        alice_b_scope_a=_runtime_store(
            config,
            tenant_id=config.tenant_b,
            principal=config.alice,
            scope=config.scope_a,
        ),
    )


def _create_claim(
    store: PostgresStore,
    config: PgConfig,
    *,
    label: str,
    scope: str,
    source_agent: str,
    visibility: str,
) -> Claim:
    return store.create_claim(
        f"{config.run_id}:{label}",
        [
            CitationInput(
                source="postgres-rls-integration",
                locator=config.run_id,
                excerpt=label,
            )
        ],
        idempotency_key=f"{config.run_id}:{label}",
        scope=scope,
        tenant_id=store.tenant_id,
        source_agent=source_agent,
        visibility=visibility,
    )


@pytest.fixture(scope="module")
def seed_rows(
    prepared_database: PreparedDatabase,
    runtime_stores: RuntimeStores,
) -> SeedRows:
    config = prepared_database.config
    alice_a = runtime_stores.alice_a_scope_a
    public_a = _create_claim(
        alice_a,
        config,
        label="public-a",
        scope=config.scope_a,
        source_agent=config.alice,
        visibility="public",
    )
    private_a = _create_claim(
        alice_a,
        config,
        label="private-a",
        scope=config.scope_a,
        source_agent=config.alice,
        visibility="private",
    )
    private_target = _create_claim(
        alice_a,
        config,
        label="private-a-target",
        scope=config.scope_a,
        source_agent=config.alice,
        visibility="private",
    )
    scope_b = _create_claim(
        runtime_stores.alice_a_scope_b,
        config,
        label="scope-b",
        scope=config.scope_b,
        source_agent=config.alice,
        visibility="public",
    )
    tenant_b = _create_claim(
        runtime_stores.alice_b_scope_a,
        config,
        label="tenant-b",
        scope=config.scope_a,
        source_agent=config.alice,
        visibility="public",
    )
    link = alice_a.add_claim_link(private_a.id, private_target.id, "derived_from")
    return SeedRows(public_a, private_a, private_target, scope_b, tenant_b, link)


def _visible_ids(store: PostgresStore) -> set[int]:
    return {claim.id for claim in store.list_claims(limit=100)}


def _assert_rls_denied(action: Callable[[], object]) -> None:
    with pytest.raises(Exception) as caught:  # noqa: B017 - driver class is optional
        action()
    assert getattr(caught.value, "sqlstate", None) == "42501", (
        "expected PostgreSQL insufficient_privilege (42501), got "
        f"{type(caught.value).__name__}"
    )


def _read_runtime_catalog(
    conn: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], set[tuple[str, str]]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_user,
                   current_setting('memorymaster.tenant_id', true) AS tenant_id,
                   current_setting('memorymaster.principal', true) AS principal,
                   current_setting('memorymaster.allowed_scopes', true) AS scopes,
                   has_schema_privilege(current_user, current_schema(), 'CREATE') AS can_create
            """
        )
        authority = cur.fetchone()
        cur.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   pg_get_userbyid(c.relowner) AS owner_name,
                   pg_has_role(current_user, c.relowner, 'MEMBER') AS owner_member,
                   has_table_privilege(current_user, c.oid, 'TRUNCATE') AS can_truncate,
                   has_table_privilege(current_user, c.oid, 'REFERENCES') AS can_references,
                   has_table_privilege(current_user, c.oid, 'TRIGGER') AS can_trigger,
                   has_table_privilege(current_user, c.oid, 'SELECT') AS can_select,
                   has_table_privilege(current_user, c.oid, 'INSERT') AS can_insert,
                   has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
                   has_any_column_privilege(current_user, c.oid, 'UPDATE')
                       AS can_update_any_column,
                   has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relname = ANY(%s)
            """,
            (list(POSTGRES_PROTECTED_TABLES),),
        )
        tables = {row["relname"]: row for row in cur.fetchall()}
        cur.execute(
            """
            SELECT tablename, policyname, permissive, roles, cmd
            FROM pg_policies
            WHERE schemaname = current_schema() AND tablename = ANY(%s)
            """,
            (list(POSTGRES_PROTECTED_TABLES),),
        )
        policies = {(row["tablename"], row["policyname"]) for row in cur.fetchall()}
    if authority is None:
        raise RuntimeError("PostgreSQL runtime authority could not be read.")
    return authority, tables, policies


def _assert_runtime_catalog(
    prepared: PreparedDatabase,
    authority: dict[str, Any],
    tables: dict[str, dict[str, Any]],
    policies: set[tuple[str, str]],
) -> None:
    config = prepared.config
    assert authority["current_user"] == prepared.app_role
    assert authority["tenant_id"] == config.tenant_a
    assert authority["principal"] == config.alice
    assert config.scope_a in authority["scopes"]
    assert authority["can_create"] is False
    assert set(tables) == set(POSTGRES_PROTECTED_TABLES)
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in tables.values())
    assert all(not row["owner_member"] for row in tables.values())
    assert all(
        not row["can_truncate"]
        and not row["can_references"]
        and not row["can_trigger"]
        for row in tables.values()
    )
    assert all(row["owner_name"] != prepared.app_role for row in tables.values())
    events = tables["events"]
    assert events["can_select"]
    assert events["can_insert"]
    assert not events["can_update"]
    assert not events["can_update_any_column"]
    assert not events["can_delete"]
    for table in POSTGRES_TENANT_POLICY_TABLES:
        for command, policy in POSTGRES_COMMAND_POLICIES.items():
            assert (table, policy) in policies
            assert (table, POSTGRES_PERMIT_POLICIES[command]) in policies
    for table in POSTGRES_TEAM_DENY_TABLES:
        assert (table, "memorymaster_team_deny") in policies
        assert not tables[table]["can_insert"]
        assert not tables[table]["can_update"]
        assert not tables[table]["can_delete"]


def _assert_authority_clears_after_commit(conn: Any) -> None:
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_setting('memorymaster.tenant_id', true) AS tenant_id,
                   current_setting('memorymaster.principal', true) AS principal,
                   current_setting('memorymaster.allowed_scopes', true) AS scopes
            """
        )
        cleared = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS count FROM claims")
        unbound_count = int(cur.fetchone()["count"])
    assert all(cleared[name] in (None, "") for name in ("tenant_id", "principal", "scopes"))
    assert unbound_count == 0


def test_admin_applies_migrations_and_team_runtime_cannot_init(
    prepared_database: PreparedDatabase,
    runtime_stores: RuntimeStores,
) -> None:
    expected = {migration.version for migration in discover_migrations()}
    assert expected <= prepared_database.applied_versions
    assert prepared_database.admin_role != prepared_database.app_role
    with pytest.raises(PermissionError, match="cannot initialize or migrate"):
        runtime_stores.alice_a_scope_a.init_db()


def test_runtime_catalog_and_transaction_local_authority(
    prepared_database: PreparedDatabase,
    runtime_stores: RuntimeStores,
) -> None:
    conn = runtime_stores.alice_a_scope_a.connect()
    try:
        authority, tables, policies = _read_runtime_catalog(conn)
        _assert_runtime_catalog(prepared_database, authority, tables, policies)
        _assert_authority_clears_after_commit(conn)
    finally:
        conn.close()


def test_claim_visibility_isolated_by_tenant_scope_and_private_principal(
    runtime_stores: RuntimeStores,
    seed_rows: SeedRows,
) -> None:
    assert _visible_ids(runtime_stores.alice_a_scope_a) == {
        seed_rows.public_a.id,
        seed_rows.private_a.id,
        seed_rows.private_a_target.id,
    }
    assert _visible_ids(runtime_stores.bob_a_scope_a) == {seed_rows.public_a.id}
    assert _visible_ids(runtime_stores.alice_a_scope_b) == {seed_rows.scope_b.id}
    assert _visible_ids(runtime_stores.alice_b_scope_a) == {seed_rows.tenant_b.id}


def test_direct_ids_citations_and_events_do_not_bypass_rls(
    runtime_stores: RuntimeStores,
    seed_rows: SeedRows,
) -> None:
    owner = runtime_stores.alice_a_scope_a
    bob = runtime_stores.bob_a_scope_a
    tenant_b = runtime_stores.alice_b_scope_a
    assert owner.get_claim(seed_rows.private_a.id) is not None
    assert len(owner.list_citations(seed_rows.private_a.id)) == 1
    assert owner.list_events(claim_id=seed_rows.private_a.id)
    assert bob.get_claim(seed_rows.public_a.id) is not None
    assert len(bob.list_citations(seed_rows.public_a.id)) == 1
    assert bob.list_events(claim_id=seed_rows.public_a.id)

    for outsider in (bob, tenant_b):
        assert outsider.get_claim(seed_rows.private_a.id) is None
        assert outsider.list_citations(seed_rows.private_a.id) == []
        assert outsider.list_events(claim_id=seed_rows.private_a.id) == []
    assert seed_rows.private_a.human_id is not None
    assert bob.get_claim_by_human_id(seed_rows.private_a.human_id) is None
    with pytest.raises(ValueError, match="No claim found"):
        bob.resolve_claim_id(seed_rows.private_a.human_id)
    assert bob.resolve_claim_id(seed_rows.private_a.id) == seed_rows.private_a.id
    assert bob.get_claim(bob.resolve_claim_id(seed_rows.private_a.id)) is None


def test_claim_links_require_visibility_of_both_endpoints(
    runtime_stores: RuntimeStores,
    seed_rows: SeedRows,
) -> None:
    owner_links = runtime_stores.alice_a_scope_a.get_claim_links(seed_rows.private_a.id)
    assert [link.id for link in owner_links] == [seed_rows.private_link.id]
    assert runtime_stores.bob_a_scope_a.get_claim_links(seed_rows.private_a.id) == []
    assert runtime_stores.alice_b_scope_a.get_claim_links(seed_rows.private_a.id) == []


def _assert_claim_create_denials(config: PgConfig, alice: PostgresStore) -> None:
    _assert_rls_denied(
        lambda: _create_claim(
            alice,
            config,
            label="forbidden-scope",
            scope=config.scope_b,
            source_agent=config.alice,
            visibility="public",
        )
    )
    _assert_rls_denied(
        lambda: _create_claim(
            alice,
            config,
            label="forbidden-private-owner",
            scope=config.scope_a,
            source_agent=config.bob,
            visibility="private",
        )
    )
    _assert_rls_denied(
        lambda: _create_claim(
            alice,
            config,
            label="forbidden-public-owner",
            scope=config.scope_a,
            source_agent=config.bob,
            visibility="public",
        )
    )
    with pytest.raises(PermissionError, match="does not match"):
        alice.create_claim(
            f"{config.run_id}:forbidden-tenant",
            [CitationInput(source="postgres-rls-integration")],
            scope=config.scope_a,
            tenant_id=config.tenant_b,
            source_agent=config.alice,
            visibility="public",
        )


def _insert_cross_tenant_citation(
    alice: PostgresStore,
    config: PgConfig,
    claim_id: int,
) -> None:
    with alice.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO citations (claim_id, source, locator, excerpt, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                claim_id,
                "postgres-rls-integration",
                config.run_id,
                "cross-tenant-write",
                datetime.now(timezone.utc),
            ),
        )


def _assert_hidden_row_write_denials(
    config: PgConfig,
    stores: RuntimeStores,
    rows: SeedRows,
) -> None:
    alice = stores.alice_a_scope_a
    bob = stores.bob_a_scope_a
    private_before = alice.get_claim(rows.private_a.id)
    assert private_before is not None
    original_confidence = private_before.confidence
    with pytest.raises(ValueError, match="does not exist"):
        bob.set_confidence(rows.private_a.id, 0.99, details="direct-id-write")
    private_after = alice.get_claim(rows.private_a.id)
    assert private_after is not None
    assert private_after.confidence == original_confidence
    public_before = alice.get_claim(rows.public_a.id)
    assert public_before is not None
    with pytest.raises(ValueError, match="does not exist"):
        bob.set_confidence(rows.public_a.id, 0.99, details="public-cross-owner-write")
    public_after = alice.get_claim(rows.public_a.id)
    assert public_after is not None
    assert public_after.confidence == public_before.confidence
    with pytest.raises(ValueError, match="does not exist"):
        alice.record_event(
            claim_id=rows.tenant_b.id,
            event_type="audit",
            details="cross-tenant-event",
        )
    _assert_rls_denied(
        lambda: alice.add_claim_link(
            rows.private_a.id,
            rows.tenant_b.id,
            "relates_to",
        )
    )
    _assert_rls_denied(
        lambda: _insert_cross_tenant_citation(alice, config, rows.tenant_b.id)
    )
    assert len(stores.alice_b_scope_a.list_citations(rows.tenant_b.id)) == 1


def test_forbidden_writes_are_rejected_or_have_no_effect(
    prepared_database: PreparedDatabase,
    runtime_stores: RuntimeStores,
    seed_rows: SeedRows,
) -> None:
    config = prepared_database.config
    _assert_claim_create_denials(config, runtime_stores.alice_a_scope_a)
    _assert_hidden_row_write_denials(config, runtime_stores, seed_rows)


def _execute_event_mutation(
    store: PostgresStore,
    statement: str,
    claim_id: int,
) -> None:
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute(statement, (claim_id,))


@pytest.mark.parametrize(
    "statement",
    (
        "UPDATE events SET details = 'forbidden-tamper' WHERE claim_id = %s",
        "DELETE FROM events WHERE claim_id = %s",
    ),
    ids=("update", "delete"),
)
def test_app_role_cannot_update_or_delete_events(
    runtime_stores: RuntimeStores,
    seed_rows: SeedRows,
    statement: str,
) -> None:
    _assert_rls_denied(
        lambda: _execute_event_mutation(
            runtime_stores.alice_a_scope_a,
            statement,
            seed_rows.public_a.id,
        )
    )


def _set_replacement_reference(
    store: PostgresStore,
    old_claim_id: int,
    replacement_id: int,
) -> None:
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE claims SET replaced_by_claim_id = %s WHERE id = %s",
            (replacement_id, old_claim_id),
        )


def test_supersession_reference_guard_rejects_hidden_and_foreign_targets(
    prepared_database: PreparedDatabase,
    runtime_stores: RuntimeStores,
    seed_rows: SeedRows,
) -> None:
    config = prepared_database.config
    bob_public = _create_claim(
        runtime_stores.bob_a_scope_a,
        config,
        label="bob-public-supersession-target",
        scope=config.scope_a,
        source_agent=config.bob,
        visibility="public",
    )
    owner = runtime_stores.alice_a_scope_a
    for target_id in (
        seed_rows.private_a.id,
        seed_rows.scope_b.id,
        seed_rows.tenant_b.id,
        bob_public.id,
    ):
        _assert_rls_denied(
            lambda target_id=target_id: _set_replacement_reference(
                owner,
                seed_rows.public_a.id,
                target_id,
            )
        )
    unchanged = owner.get_claim(seed_rows.public_a.id, include_citations=False)
    assert unchanged is not None
    assert unchanged.replaced_by_claim_id is None


def test_canonical_supersession_commits_reciprocal_pair_and_one_event(
    prepared_database: PreparedDatabase,
    runtime_stores: RuntimeStores,
) -> None:
    config = prepared_database.config
    store = runtime_stores.alice_a_scope_a
    old = _create_claim(
        store,
        config,
        label="atomic-supersession-old",
        scope=config.scope_a,
        source_agent=config.alice,
        visibility="public",
    )
    replacement = _create_claim(
        store,
        config,
        label="atomic-supersession-new",
        scope=config.scope_a,
        source_agent=config.alice,
        visibility="public",
    )

    store.mark_superseded(old.id, replacement.id, "integration atomicity")

    refreshed_old = store.get_claim(old.id, include_citations=False)
    refreshed_replacement = store.get_claim(
        replacement.id,
        include_citations=False,
    )
    events = store.list_events(claim_id=old.id, event_type="supersession")
    assert refreshed_old.status == "superseded"
    assert refreshed_old.replaced_by_claim_id == replacement.id
    assert refreshed_replacement.supersedes_claim_id == old.id
    assert len(events) == 1


def _tenant_event_rows_as_admin(
    prepared: PreparedDatabase,
) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(
        prepared.config.admin_dsn,
        row_factory=dict_row,
    ) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, claim_id, event_type, from_status, to_status, details,
                   payload_json, created_at, prev_event_hash, event_hash, hash_algo,
                   tenant_id, tenant_prev_event_hash, tenant_event_hash, tenant_hash_algo
            FROM public.events
            WHERE tenant_id = %s
            ORDER BY id ASC
            """,
            (prepared.config.tenant_a,),
        )
        return list(cur.fetchall())


def test_tenant_event_chain_does_not_fork_across_rls_views(
    prepared_database: PreparedDatabase,
    runtime_stores: RuntimeStores,
    seed_rows: SeedRows,
) -> None:
    """Cross-scope/private appends must share one tenant-global chain head."""
    config = prepared_database.config
    bob_private = _create_claim(
        runtime_stores.bob_a_scope_a,
        config,
        label="bob-private-chain",
        scope=config.scope_a,
        source_agent=config.bob,
        visibility="private",
    )
    for store, claim, label in (
        (runtime_stores.alice_a_scope_a, seed_rows.private_a, "alice-private"),
        (runtime_stores.alice_a_scope_b, seed_rows.scope_b, "alice-scope-b"),
        (runtime_stores.bob_a_scope_a, bob_private, "bob-private"),
    ):
        store.record_event(
            claim_id=claim.id,
            event_type="audit",
            details=f"{config.run_id}:{label}:chain-proof",
        )

    rows = _tenant_event_rows_as_admin(prepared_database)
    claim_ids = {int(row["claim_id"]) for row in rows if row["claim_id"] is not None}
    assert {
        seed_rows.private_a.id,
        seed_rows.scope_b.id,
        bob_private.id,
    } <= claim_ids
    assert PostgresStore._event_chain_issues(rows, limit=500) == []
    assert PostgresStore._tenant_event_chain_issues(rows, limit=500) == []


def test_principal_local_claim_identity_matrix(
    prepared_database: PreparedDatabase,
    runtime_stores: RuntimeStores,
) -> None:
    config = prepared_database.config
    citation = [CitationInput(source="postgres-principal-identity", locator=config.run_id)]

    def create(store: PostgresStore, key: str, visibility: str) -> Claim:
        return store.create_claim(
            f"{config.run_id}:same identity payload",
            citation,
            idempotency_key=f"{config.run_id}:{key}",
            subject=f"{config.run_id}:identity-subject",
            predicate="uses",
            scope=config.scope_a,
            tenant_id=config.tenant_a,
            source_agent=store.principal,
            visibility=visibility,
        )

    alice = runtime_stores.alice_a_scope_a
    bob = runtime_stores.bob_a_scope_a
    alice_private = create(alice, "private-shared", "private")
    bob_private = create(bob, "private-shared", "private")
    alice_public = create(alice, "public-shared", "public")
    bob_public = create(bob, "public-shared", "public")
    public_cross = create(alice, "cross-visibility", "public")
    private_cross = create(alice, "cross-visibility", "private")

    assert alice_private.id != bob_private.id
    assert alice_private.human_id == bob_private.human_id
    assert bob_public.id == alice_public.id
    assert private_cross.id != public_cross.id
    assert private_cross.human_id == public_cross.human_id


def test_public_claim_identity_is_scope_local_and_lookups_require_exact_scope(
    prepared_database: PreparedDatabase,
    runtime_stores: RuntimeStores,
) -> None:
    config = prepared_database.config
    label = "public-cross-scope-shared-identity"
    idempotency_key = f"{config.run_id}:{label}"
    scope_a = _create_claim(
        runtime_stores.alice_a_scope_a,
        config,
        label=label,
        scope=config.scope_a,
        source_agent=config.alice,
        visibility="public",
    )
    scope_b = _create_claim(
        runtime_stores.alice_a_scope_b,
        config,
        label=label,
        scope=config.scope_b,
        source_agent=config.alice,
        visibility="public",
    )

    assert scope_b.id != scope_a.id
    assert scope_a.human_id is not None
    assert scope_b.human_id == scope_a.human_id
    assert "~" not in scope_a.human_id

    multi_scope = PostgresStore(
        config.app_dsn,
        tenant_id=config.tenant_a,
        require_tenant=True,
        principal=config.alice,
        allowed_scopes=(config.scope_a, config.scope_b),
    )
    assert multi_scope.get_claim_by_idempotency_key(
        idempotency_key,
        scope=config.scope_a,
    ).id == scope_a.id
    assert multi_scope.get_claim_by_idempotency_key(
        idempotency_key,
        scope=config.scope_b,
    ).id == scope_b.id
    assert multi_scope.get_claim_by_human_id(
        scope_a.human_id,
        scope=config.scope_a,
    ).id == scope_a.id
    assert multi_scope.get_claim_by_human_id(
        scope_a.human_id,
        scope=config.scope_b,
    ).id == scope_b.id
    assert multi_scope.resolve_claim_id(
        scope_a.human_id,
        scope=config.scope_a,
    ) == scope_a.id
    assert multi_scope.resolve_claim_id(
        scope_a.human_id,
        scope=config.scope_b,
    ) == scope_b.id

    with pytest.raises(ValueError, match="exact claim scope"):
        multi_scope.get_claim_by_idempotency_key(idempotency_key)
    with pytest.raises(ValueError, match="exact claim scope"):
        multi_scope.get_claim_by_human_id(scope_a.human_id)
