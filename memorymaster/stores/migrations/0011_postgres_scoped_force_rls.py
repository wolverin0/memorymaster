"""Force PostgreSQL RLS with tenant, principal, and scope authorization."""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

VERSION = 11
DESCRIPTION = "Force scoped PostgreSQL row-level security policies"

_CURRENT_TENANT = "NULLIF(current_setting('memorymaster.tenant_id', true), '')"
_CURRENT_PRINCIPAL = "NULLIF(current_setting('memorymaster.principal', true), '')"
_ALLOWED_SCOPES = (
    "COALESCE(NULLIF(current_setting('memorymaster.allowed_scopes', true), ''), "
    "'[]')::jsonb"
)

_DENY_ALL_TABLES = (
    "action_proposals",
    "external_sources",
    "source_items",
    "evidence_items",
    "media_retry_queue",
    "query_cache",
    "miner_state",
    "rule_stats",
)

_KNOWN_POLICY_NAMES = (
    "memorymaster_rls_permit",
    "memorymaster_tenant_restrict",
    "memorymaster_team_deny",
    "memorymaster_tenant_select",
    "memorymaster_tenant_insert",
    "memorymaster_tenant_update",
    "memorymaster_tenant_delete",
    "memorymaster_tenant_select_permit",
    "memorymaster_tenant_insert_permit",
    "memorymaster_tenant_update_permit",
    "memorymaster_tenant_delete_permit",
)

_COMMAND_POLICIES = {
    "SELECT": "memorymaster_tenant_select",
    "INSERT": "memorymaster_tenant_insert",
    "UPDATE": "memorymaster_tenant_update",
    "DELETE": "memorymaster_tenant_delete",
}
_POLICY_FIELDS = (
    "schemaname",
    "tablename",
    "policyname",
    "permissive",
    "roles",
    "cmd",
    "qual",
    "with_check",
)
_POLICY_MANIFEST_PREFIX = "memorymaster.rls/v1;manifest=0011;sha256="

_EVENT_HEAD_FUNCTION = """
CREATE OR REPLACE FUNCTION public.memorymaster_event_chain_head()
RETURNS TABLE (global_event_hash TEXT, tenant_event_hash TEXT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    bound_tenant TEXT := NULLIF(current_setting('memorymaster.tenant_id', true), '');
BEGIN
    IF bound_tenant IS NULL THEN
        RAISE EXCEPTION 'event chain head requires bound tenant authority'
            USING ERRCODE = '42501';
    END IF;
    RETURN QUERY SELECT
        (
            SELECT event.event_hash
            FROM public.events AS event
            WHERE event.tenant_id = bound_tenant
              AND event.event_hash IS NOT NULL
              AND event.hash_algo = 'sha256-tenant-v2'
            ORDER BY event.id DESC LIMIT 1
        ),
        (
            SELECT event.tenant_event_hash
            FROM public.events AS event
            WHERE event.tenant_id = bound_tenant
              AND event.tenant_event_hash IS NOT NULL
            ORDER BY event.id DESC LIMIT 1
        );
END;
$$
""".strip()

_EVENT_HEAD_REVOKE = (
    "REVOKE ALL ON FUNCTION public.memorymaster_event_chain_head() FROM PUBLIC"
)


def _claim_authority_predicate(alias: str) -> str:
    return (
        f"{_CURRENT_PRINCIPAL} IS NOT NULL "
        f"AND {_CURRENT_TENANT} IS NOT NULL "
        f"AND {alias}.tenant_id = {_CURRENT_TENANT} "
        f"AND jsonb_typeof({_ALLOWED_SCOPES}) = 'array' "
        f"AND {_ALLOWED_SCOPES} ? {alias}.scope"
    )


def _claim_read_predicate(alias: str) -> str:
    return (
        f"{_claim_authority_predicate(alias)} "
        f"AND ({alias}.visibility = 'public' "
        f"OR ({alias}.visibility = 'private' "
        f"AND {alias}.source_agent = {_CURRENT_PRINCIPAL}))"
    )


def _claim_write_predicate(alias: str) -> str:
    return (
        f"{_claim_authority_predicate(alias)} "
        f"AND {alias}.source_agent = {_CURRENT_PRINCIPAL} "
        f"AND {alias}.visibility IN ('public', 'private')"
    )


def _claim_exists(
    table: str,
    claim_column: str,
    alias: str,
    *,
    write: bool,
) -> str:
    predicate = (
        _claim_write_predicate(alias)
        if write
        else _claim_read_predicate(alias)
    )
    return (
        f"EXISTS (SELECT 1 FROM claims AS {alias} "
        f"WHERE {alias}.id = {table}.{claim_column} "
        f"AND {predicate})"
    )


def _claim_pair_predicate(
    table: str,
    left_column: str,
    right_column: str,
    left_alias: str,
    right_alias: str,
    *,
    write: bool,
) -> str:
    return (
        f"{_claim_exists(table, left_column, left_alias, write=write)} AND "
        f"{_claim_exists(table, right_column, right_alias, write=write)}"
    )


def _event_predicate(*, write: bool) -> str:
    return (
        f"{_CURRENT_PRINCIPAL} IS NOT NULL "
        f"AND {_CURRENT_TENANT} IS NOT NULL "
        f"AND events.tenant_id = {_CURRENT_TENANT} "
        "AND (events.claim_id IS NULL OR "
        f"{_claim_exists('events', 'claim_id', 'mm_claim', write=write)})"
    )


_READ_PREDICATES = {
    "claims": _claim_read_predicate("claims"),
    "citations": _claim_exists(
        "citations", "claim_id", "mm_claim", write=False
    ),
    "events": _event_predicate(write=False),
    "claim_links": _claim_pair_predicate(
        "claim_links",
        "source_id",
        "target_id",
        "mm_source",
        "mm_target",
        write=False,
    ),
    "claim_embeddings": _claim_exists(
        "claim_embeddings", "claim_id", "mm_claim", write=False
    ),
    "contradiction_verdicts": _claim_pair_predicate(
        "contradiction_verdicts",
        "claim_a_id",
        "claim_b_id",
        "mm_a",
        "mm_b",
        write=False,
    ),
    "mcp_usage": (
        f"{_CURRENT_PRINCIPAL} IS NOT NULL "
        f"AND mcp_usage.tenant_id = {_CURRENT_TENANT}"
    ),
}

_WRITE_PREDICATES = {
    "claims": _claim_write_predicate("claims"),
    "citations": _claim_exists(
        "citations", "claim_id", "mm_claim", write=True
    ),
    "events": _event_predicate(write=True),
    "claim_links": _claim_pair_predicate(
        "claim_links",
        "source_id",
        "target_id",
        "mm_source",
        "mm_target",
        write=True,
    ),
    "claim_embeddings": _claim_exists(
        "claim_embeddings", "claim_id", "mm_claim", write=True
    ),
    "contradiction_verdicts": _claim_pair_predicate(
        "contradiction_verdicts",
        "claim_a_id",
        "claim_b_id",
        "mm_a",
        "mm_b",
        write=True,
    ),
    "mcp_usage": (
        f"{_CURRENT_PRINCIPAL} IS NOT NULL "
        f"AND mcp_usage.tenant_id = {_CURRENT_TENANT}"
    ),
}


def _prepare_table(cur, table: str) -> None:
    # Identifiers come only from the immutable constants above, never user input.
    cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for old_policy in _KNOWN_POLICY_NAMES:
        cur.execute(f"DROP POLICY IF EXISTS {old_policy} ON {table}")


def _install_command_policy(
    cur,
    table: str,
    command: str,
    predicate: str,
) -> None:
    policy_name = _COMMAND_POLICIES[command]
    if command == "INSERT":
        clauses = f"WITH CHECK ({predicate})"
    elif command == "UPDATE":
        clauses = f"USING ({predicate}) WITH CHECK ({predicate})"
    else:
        clauses = f"USING ({predicate})"
    cur.execute(
        f"CREATE POLICY {policy_name}_permit ON {table} "
        f"AS PERMISSIVE FOR {command} TO PUBLIC {clauses}"
    )
    cur.execute(
        f"CREATE POLICY {policy_name} ON {table} "
        f"AS RESTRICTIVE FOR {command} TO PUBLIC {clauses}"
    )


def _install_scoped_policies(cur, table: str) -> None:
    _prepare_table(cur, table)
    _install_command_policy(cur, table, "SELECT", _READ_PREDICATES[table])
    for command in ("INSERT", "UPDATE", "DELETE"):
        _install_command_policy(cur, table, command, _WRITE_PREDICATES[table])


def _install_deny_policy(cur, table: str) -> None:
    _prepare_table(cur, table)
    cur.execute(
        f"CREATE POLICY memorymaster_team_deny ON {table} "
        "AS RESTRICTIVE FOR ALL TO PUBLIC USING (FALSE) WITH CHECK (FALSE)"
    )


def _canonical_policy_payload(rows: Iterable[dict[str, object]]) -> str:
    payload: list[dict[str, object]] = []
    for policy in rows:
        row = {field: policy.get(field) for field in _POLICY_FIELDS}
        roles = row["roles"]
        if isinstance(roles, (list, tuple, set, frozenset)):
            row["roles"] = sorted(str(role) for role in roles)
        payload.append(row)
    payload.sort(
        key=lambda row: (
            str(row["schemaname"]),
            str(row["tablename"]),
            str(row["policyname"]),
        )
    )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _stamp_policy_manifest(cur) -> None:
    governed_tables = list(_READ_PREDICATES) + list(_DENY_ALL_TABLES)
    cur.execute(
        """
        SELECT schemaname, tablename, policyname, permissive,
               roles, cmd, qual, with_check
        FROM pg_policies
        WHERE schemaname = current_schema() AND tablename = ANY(%s)
        """,
        (governed_tables,),
    )
    rows = list(cur.fetchall())
    expected_count = len(_READ_PREDICATES) * 8 + len(_DENY_ALL_TABLES)
    if len(rows) != expected_count:
        raise RuntimeError("Postgres RLS policy manifest inventory is incomplete.")
    payload = _canonical_policy_payload(rows).encode("utf-8")
    comment = f"{_POLICY_MANIFEST_PREFIX}{hashlib.sha256(payload).hexdigest()}"
    # The value is entirely internal fixed text + a lowercase SHA-256 digest.
    # PostgreSQL DDL cannot use psycopg server-side bind parameters here.
    cur.execute(
        "COMMENT ON POLICY memorymaster_tenant_select ON claims "
        f"IS '{comment}'"
    )


def apply_sqlite(_conn) -> None:
    """RLS is PostgreSQL-specific; SQLite remains predicate-isolated."""


def apply_postgres(conn) -> None:
    try:
        with conn.cursor() as cur:
            for table in _READ_PREDICATES:
                _install_scoped_policies(cur, table)
            for table in _DENY_ALL_TABLES:
                _install_deny_policy(cur, table)
            cur.execute("DROP TRIGGER IF EXISTS claims_gen_ins_del ON claims")
            cur.execute("DROP TRIGGER IF EXISTS claims_gen_upd ON claims")
            cur.execute(_EVENT_HEAD_FUNCTION)
            cur.execute(_EVENT_HEAD_REVOKE)
            _stamp_policy_manifest(cur)
        conn.commit()
    except Exception:
        rollback = getattr(conn, "rollback", None)
        if callable(rollback):
            rollback()
        raise
