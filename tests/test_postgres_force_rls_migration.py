"""Adversarial contract tests for PostgreSQL scoped FORCE-RLS migration v0011."""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import sqlite3
from typing import Sequence

from memorymaster.stores.migrations import discover_migrations


SCOPED_TABLES = {
    "claims",
    "citations",
    "events",
    "claim_links",
    "claim_embeddings",
    "contradiction_verdicts",
    "mcp_usage",
}

DENY_ALL_TABLES = {
    "action_proposals",
    "external_sources",
    "source_items",
    "evidence_items",
    "media_retry_queue",
    "query_cache",
    "miner_state",
    "rule_stats",
}

GOVERNED_TABLES = SCOPED_TABLES | DENY_ALL_TABLES

COMMAND_POLICIES = {
    "SELECT": "memorymaster_tenant_select",
    "INSERT": "memorymaster_tenant_insert",
    "UPDATE": "memorymaster_tenant_update",
    "DELETE": "memorymaster_tenant_delete",
}

PERMIT_POLICIES = {
    command: f"{name}_permit" for command, name in COMMAND_POLICIES.items()
}


POLICY_FIELDS = (
    "schemaname",
    "tablename",
    "policyname",
    "permissive",
    "roles",
    "cmd",
    "qual",
    "with_check",
)


def _parenthesized_clause(sql: str, marker: str) -> str | None:
    start = sql.find(marker)
    if start < 0:
        return None
    start += len(marker)
    depth = 1
    for index in range(start, len(sql)):
        character = sql[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return sql[start:index]
    raise AssertionError(f"unbalanced policy clause: {sql}")


def _parse_created_policy(sql: str) -> dict[str, object] | None:
    match = re.match(
        r"CREATE POLICY (\S+) ON (\S+) AS (PERMISSIVE|RESTRICTIVE) "
        r"FOR (SELECT|INSERT|UPDATE|DELETE|ALL) TO PUBLIC (.*)",
        sql,
    )
    if match is None:
        return None
    name, table, mode, command, clauses = match.groups()
    return {
        "schemaname": "public",
        "tablename": table,
        "policyname": name,
        "permissive": mode,
        "roles": ["public"],
        "cmd": command,
        "qual": _parenthesized_clause(clauses, "USING ("),
        "with_check": _parenthesized_clause(clauses, "WITH CHECK ("),
    }


def _canonical_policy_payload(policies: Sequence[dict[str, object]]) -> str:
    rows: list[dict[str, object]] = []
    for policy in policies:
        row = {field: policy.get(field) for field in POLICY_FIELDS}
        roles = row["roles"]
        if isinstance(roles, (list, tuple, set, frozenset)):
            row["roles"] = sorted(str(role) for role in roles)
        rows.append(row)
    rows.sort(
        key=lambda row: (
            str(row["schemaname"]),
            str(row["tablename"]),
            str(row["policyname"]),
        )
    )
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


class RecordingCursor:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection
        self._rows: list[dict[str, object]] = []

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        normalized = " ".join(sql.split())
        bound = tuple(params) if isinstance(params, (list, tuple)) else ()
        recorded = normalized
        if len(bound) == 1 and "%s" in recorded:
            recorded = recorded.replace("%s", repr(bound[0]), 1)
        self.connection.statements.append(recorded)
        if normalized.startswith("COMMENT ON POLICY") and bound:
            self.connection.parameterized_comment_calls.append((normalized, bound))
        if "FROM pg_policies" in normalized:
            self.connection.catalog_reads += 1
            self._rows = self.connection.policy_rows
            return
        created = _parse_created_policy(normalized)
        if created is not None:
            key = (str(created["tablename"]), str(created["policyname"]))
            self.connection.policies[key] = created
            return
        dropped = re.match(r"DROP POLICY IF EXISTS (\S+) ON (\S+)", normalized)
        if dropped is not None:
            name, table = dropped.groups()
            self.connection.policies.pop((table, name), None)

    def fetchall(self) -> list[dict[str, object]]:
        return [dict(row) for row in self._rows]

    def fetchone(self) -> dict[str, object] | None:
        return dict(self._rows[0]) if self._rows else None


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.policies: dict[tuple[str, str], dict[str, object]] = {}
        self.catalog_reads = 0
        self.parameterized_comment_calls: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self)

    @property
    def policy_rows(self) -> list[dict[str, object]]:
        rows = [dict(row) for _, row in sorted(self.policies.items())]
        # pg_policies exposes server-deparsed expressions, not the emitted DDL text.
        # Deliberately model a harmless deparser difference so source-SQL hashing
        # cannot satisfy the manifest test while merely performing a decoy read.
        for row in rows:
            for field in ("qual", "with_check"):
                expression = row[field]
                if expression not in {None, "FALSE"}:
                    row[field] = f"({expression})"
        return rows

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _migration():
    return importlib.import_module(
        "memorymaster.stores.migrations.0011_postgres_scoped_force_rls"
    )


def _apply_postgres_migration() -> RecordingConnection:
    conn = RecordingConnection()
    _migration().apply_postgres(conn)
    return conn


def _policy(conn: RecordingConnection, table: str, name: str) -> str:
    prefix = f"CREATE POLICY {name} ON {table} "
    return next(statement for statement in conn.statements if statement.startswith(prefix))


def _command_policy(conn: RecordingConnection, table: str, command: str) -> str:
    policy = _policy(conn, table, COMMAND_POLICIES[command])
    assert f"FOR {command} TO PUBLIC" in policy
    return policy


def _permit_policy(conn: RecordingConnection, table: str, command: str) -> str:
    policy = _policy(conn, table, PERMIT_POLICIES[command])
    assert f"AS PERMISSIVE FOR {command} TO PUBLIC" in policy
    return policy


def test_scoped_force_rls_is_immutable_migration_v0011() -> None:
    migration = next(item for item in discover_migrations() if item.version == 11)

    assert "force" in migration.description.lower()
    assert "scope" in migration.description.lower()


def test_sqlite_side_is_a_true_noop() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        _migration().apply_sqlite(conn)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        conn.close()

    assert tables == []


def test_all_governed_tables_enable_and_force_row_security() -> None:
    conn = _apply_postgres_migration()
    emitted = "\n".join(conn.statements)

    assert len(GOVERNED_TABLES) == 15
    for table in GOVERNED_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in emitted
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in emitted
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_claim_select_allows_public_or_principal_owned_private_rows() -> None:
    policy = _command_policy(_apply_postgres_migration(), "claims", "SELECT")

    assert "NULLIF(current_setting('memorymaster.principal', true), '') IS NOT NULL" in policy
    assert (
        "claims.tenant_id = NULLIF(current_setting('memorymaster.tenant_id', true), '')"
        in policy
    )
    assert (
        "COALESCE(NULLIF(current_setting('memorymaster.allowed_scopes', true), ''), "
        "'[]')::jsonb ? claims.scope"
    ) in policy
    assert (
        "jsonb_typeof(COALESCE(NULLIF(current_setting("
        "'memorymaster.allowed_scopes', true), ''), '[]')::jsonb) = 'array'"
        in policy
    )
    assert "claims.visibility = 'public'" in policy
    assert "claims.visibility = 'private'" in policy
    assert "claims.visibility <> 'private'" not in policy
    assert (
        "claims.source_agent = NULLIF(current_setting('memorymaster.principal', true), '')"
        in policy
    )
    assert "USING" in policy
    assert "WITH CHECK" not in policy


def test_claim_writes_require_principal_ownership_without_public_bypass() -> None:
    conn = _apply_postgres_migration()

    for command in {"INSERT", "UPDATE", "DELETE"}:
        policy = _command_policy(conn, "claims", command)
        assert (
            "claims.source_agent = NULLIF(current_setting('memorymaster.principal', true), '')"
            in policy
        )
        assert "? claims.scope" in policy
        assert "claims.visibility = 'public'" not in policy
        assert "claims.visibility = 'private'" not in policy
    assert "WITH CHECK" in _command_policy(conn, "claims", "INSERT")
    assert "WITH CHECK" in _command_policy(conn, "claims", "UPDATE")
    assert "WITH CHECK" not in _command_policy(conn, "claims", "DELETE")


def test_claim_write_policies_reject_unreadable_sensitive_visibility() -> None:
    conn = _apply_postgres_migration()

    for command in {"INSERT", "UPDATE", "DELETE"}:
        policy = _command_policy(conn, "claims", command)
        assert "claims.visibility IN ('public', 'private')" in policy
        assert "sensitive" not in policy


def test_single_claim_children_split_read_visibility_from_write_ownership() -> None:
    conn = _apply_postgres_migration()

    for table in {"citations", "claim_embeddings"}:
        read_policy = _command_policy(conn, table, "SELECT")
        assert f"mm_claim.id = {table}.claim_id" in read_policy
        assert "mm_claim.visibility = 'public'" in read_policy
        for command in {"INSERT", "UPDATE", "DELETE"}:
            write_policy = _command_policy(conn, table, command)
            assert f"mm_claim.id = {table}.claim_id" in write_policy
            assert "mm_claim.visibility = 'public'" not in write_policy
            assert "mm_claim.source_agent = NULLIF(current_setting('memorymaster.principal', true), '')" in write_policy


def test_action_proposals_remain_team_denied_in_phase_one() -> None:
    conn = _apply_postgres_migration()
    policy = _policy(conn, "action_proposals", "memorymaster_team_deny")

    assert "AS RESTRICTIVE FOR ALL TO PUBLIC" in policy
    assert "USING (FALSE) WITH CHECK (FALSE)" in policy
    assert not any(
        statement.startswith(f"CREATE POLICY {name} ON action_proposals ")
        for name in COMMAND_POLICIES.values()
        for statement in conn.statements
    )


def test_pair_children_require_read_access_or_write_ownership_for_both_claims() -> None:
    conn = _apply_postgres_migration()
    links = _command_policy(conn, "claim_links", "SELECT")
    verdicts = _command_policy(conn, "contradiction_verdicts", "SELECT")

    assert "mm_source.id = claim_links.source_id" in links
    assert "mm_target.id = claim_links.target_id" in links
    assert links.count("current_setting('memorymaster.allowed_scopes', true)") >= 4
    assert "mm_a.id = contradiction_verdicts.claim_a_id" in verdicts
    assert "mm_b.id = contradiction_verdicts.claim_b_id" in verdicts
    assert verdicts.count("current_setting('memorymaster.allowed_scopes', true)") >= 4
    for table in {"claim_links", "contradiction_verdicts"}:
        for command in {"INSERT", "UPDATE", "DELETE"}:
            policy = _command_policy(conn, table, command)
            assert "visibility = 'public'" not in policy
            assert policy.count("source_agent") >= 2


def test_events_split_public_reads_from_claim_owner_writes() -> None:
    conn = _apply_postgres_migration()
    policy = _command_policy(conn, "events", "SELECT")

    assert "events.tenant_id = NULLIF(current_setting('memorymaster.tenant_id', true), '')" in policy
    assert "NULLIF(current_setting('memorymaster.principal', true), '') IS NOT NULL" in policy
    assert "events.claim_id IS NULL OR EXISTS" in policy
    assert "mm_claim.id = events.claim_id" in policy
    assert "? mm_claim.scope" in policy
    assert "mm_claim.visibility = 'public'" in policy
    assert "mm_claim.visibility = 'private'" in policy
    for command in {"INSERT", "UPDATE", "DELETE"}:
        write_policy = _command_policy(conn, "events", command)
        assert "events.claim_id IS NULL OR EXISTS" in write_policy
        assert "mm_claim.visibility = 'public'" not in write_policy
        assert "mm_claim.source_agent" in write_policy


def test_mcp_usage_is_strictly_tenant_bound() -> None:
    conn = _apply_postgres_migration()
    for command in COMMAND_POLICIES:
        policy = _command_policy(conn, "mcp_usage", command)
        assert "mcp_usage.tenant_id = NULLIF(current_setting('memorymaster.tenant_id', true), '')" in policy
        assert "NULLIF(current_setting('memorymaster.principal', true), '') IS NOT NULL" in policy
        assert "tenant_id IS NOT DISTINCT FROM" not in policy


def test_untenantable_tables_keep_restrictive_deny_all_policies() -> None:
    conn = _apply_postgres_migration()
    emitted = "\n".join(conn.statements)

    for table in DENY_ALL_TABLES:
        policy = _policy(conn, table, "memorymaster_team_deny")
        assert "AS RESTRICTIVE" in policy
        assert "USING (FALSE) WITH CHECK (FALSE)" in policy
        assert f"DROP POLICY IF EXISTS memorymaster_tenant_restrict ON {table}" in emitted


def test_scoped_commands_pair_exact_permissive_and_restrictive_predicates() -> None:
    conn = _apply_postgres_migration()
    emitted = "\n".join(conn.statements)

    assert "USING (TRUE)" not in emitted
    assert "WITH CHECK (TRUE)" not in emitted
    for table in SCOPED_TABLES:
        for command in COMMAND_POLICIES:
            permit = _permit_policy(conn, table, command)
            restrict = _command_policy(conn, table, command)
            assert permit.split(" TO PUBLIC ", 1)[1] == restrict.split(" TO PUBLIC ", 1)[1]
    for table in DENY_ALL_TABLES:
        assert not any(
            statement.startswith("CREATE POLICY ") and f" ON {table} " in statement
            and "AS PERMISSIVE" in statement
            for statement in conn.statements
        )
    for table in GOVERNED_TABLES:
        assert f"DROP POLICY IF EXISTS memorymaster_rls_permit ON {table}" in emitted
    for name in (
        "memorymaster_tenant_restrict",
        *COMMAND_POLICIES.values(),
        *PERMIT_POLICIES.values(),
    ):
        assert emitted.count(f"DROP POLICY IF EXISTS {name} ON") == 15
    assert emitted.count("AS PERMISSIVE FOR SELECT TO PUBLIC") == len(SCOPED_TABLES)
    assert emitted.count("AS PERMISSIVE FOR INSERT TO PUBLIC") == len(SCOPED_TABLES)
    assert emitted.count("AS PERMISSIVE FOR UPDATE TO PUBLIC") == len(SCOPED_TABLES)
    assert emitted.count("AS PERMISSIVE FOR DELETE TO PUBLIC") == len(SCOPED_TABLES)
    assert emitted.count("AS RESTRICTIVE FOR SELECT TO PUBLIC") == len(SCOPED_TABLES)
    assert emitted.count("AS RESTRICTIVE FOR INSERT TO PUBLIC") == len(SCOPED_TABLES)
    assert emitted.count("AS RESTRICTIVE FOR UPDATE TO PUBLIC") == len(SCOPED_TABLES)
    assert emitted.count("AS RESTRICTIVE FOR DELETE TO PUBLIC") == len(SCOPED_TABLES)
    assert emitted.count("AS RESTRICTIVE FOR ALL TO PUBLIC") == len(DENY_ALL_TABLES)


def test_migration_stamps_one_policy_manifest_and_disables_cache_generation_triggers() -> None:
    conn = _apply_postgres_migration()
    emitted = "\n".join(conn.statements)
    digest = hashlib.sha256(
        _canonical_policy_payload(conn.policy_rows).encode("utf-8")
    ).hexdigest()
    expected_comment = f"memorymaster.rls/v1;manifest=0011;sha256={digest}"

    stamp_statements = [
        statement
        for statement in conn.statements
        if statement.startswith("COMMENT ON POLICY ")
        and expected_comment in statement
    ]
    assert len(stamp_statements) == 1
    assert conn.catalog_reads >= 1
    catalog_query = next(
        statement for statement in conn.statements if "FROM pg_policies" in statement
    )
    for field in POLICY_FIELDS:
        assert field in catalog_query
    assert "claims_gen_ins_del" in emitted
    assert "claims_gen_upd" in emitted
    assert emitted.count("DROP TRIGGER IF EXISTS claims_gen_") == 2


def test_policy_manifest_comment_uses_literal_ddl_not_server_side_parameters() -> None:
    conn = _apply_postgres_migration()

    assert conn.parameterized_comment_calls == []
    comment = next(
        statement
        for statement in conn.statements
        if statement.startswith("COMMENT ON POLICY ")
    )
    assert " IS 'memorymaster.rls/v1;manifest=0011;sha256=" in comment
    assert comment.endswith("'")
