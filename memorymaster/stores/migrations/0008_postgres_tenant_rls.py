"""PostgreSQL row security for tenant-owned and team-disabled data.

The application role is intentionally expected to be a non-owner role without
``BYPASSRLS``.  Owners and migration roles retain their normal administrative
access; callers requiring tenant isolation must reject those privileged roles
at the connection boundary.

Each tenant table gets an explicit permissive base plus a restrictive tenant
gate.  PostgreSQL ORs permissive policies but ANDs restrictive policies, so a
later permissive policy cannot widen the tenant boundary.  Tables whose schema
has no tenant key are denied to the team role until a future migration makes
their ownership model explicit.
"""
from __future__ import annotations

VERSION = 8
DESCRIPTION = "Postgres tenant row-level security policies"

_CURRENT_TENANT = "NULLIF(current_setting('memorymaster.tenant_id', true), '')"

_TENANT_PREDICATES = {
    "claims": f"claims.tenant_id = {_CURRENT_TENANT}",
    "mcp_usage": f"mcp_usage.tenant_id = {_CURRENT_TENANT}",
    "citations": (
        "EXISTS (SELECT 1 FROM claims AS mm_claim "
        "WHERE mm_claim.id = citations.claim_id "
        f"AND mm_claim.tenant_id = {_CURRENT_TENANT})"
    ),
    "events": (
        "events.claim_id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM claims AS mm_claim WHERE mm_claim.id = events.claim_id "
        f"AND mm_claim.tenant_id = {_CURRENT_TENANT})"
    ),
    "claim_links": (
        "EXISTS (SELECT 1 FROM claims AS mm_source "
        "WHERE mm_source.id = claim_links.source_id "
        f"AND mm_source.tenant_id = {_CURRENT_TENANT}) AND "
        "EXISTS (SELECT 1 FROM claims AS mm_target "
        "WHERE mm_target.id = claim_links.target_id "
        f"AND mm_target.tenant_id = {_CURRENT_TENANT})"
    ),
    "claim_embeddings": (
        "EXISTS (SELECT 1 FROM claims AS mm_claim "
        "WHERE mm_claim.id = claim_embeddings.claim_id "
        f"AND mm_claim.tenant_id = {_CURRENT_TENANT})"
    ),
    "action_proposals": (
        "action_proposals.claim_id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM claims AS mm_claim "
        "WHERE mm_claim.id = action_proposals.claim_id "
        f"AND mm_claim.tenant_id = {_CURRENT_TENANT})"
    ),
    "contradiction_verdicts": (
        "EXISTS (SELECT 1 FROM claims AS mm_a "
        "WHERE mm_a.id = contradiction_verdicts.claim_a_id "
        f"AND mm_a.tenant_id = {_CURRENT_TENANT}) AND "
        "EXISTS (SELECT 1 FROM claims AS mm_b "
        "WHERE mm_b.id = contradiction_verdicts.claim_b_id "
        f"AND mm_b.tenant_id = {_CURRENT_TENANT})"
    ),
}

_TEAM_DENY_TABLES = (
    "external_sources",
    "source_items",
    "evidence_items",
    "media_retry_queue",
    "query_cache",
    "miner_state",
    "rule_stats",
)


def _enable_policy(cur, table: str, policy: str, predicate: str) -> None:
    cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    cur.execute(f"DROP POLICY IF EXISTS memorymaster_rls_permit ON {table}")
    cur.execute(
        f"CREATE POLICY memorymaster_rls_permit ON {table} "
        "AS PERMISSIVE FOR ALL TO PUBLIC USING (TRUE) WITH CHECK (TRUE)"
    )
    cur.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    cur.execute(
        f"CREATE POLICY {policy} ON {table} AS RESTRICTIVE FOR ALL TO PUBLIC "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def apply_sqlite(_conn) -> None:
    """RLS is PostgreSQL-specific; SQLite isolation stays predicate-based."""


def apply_postgres(conn) -> None:
    with conn.cursor() as cur:
        for table, predicate in _TENANT_PREDICATES.items():
            _enable_policy(cur, table, "memorymaster_tenant_restrict", predicate)
        for table in _TEAM_DENY_TABLES:
            _enable_policy(cur, table, "memorymaster_team_deny", "FALSE")
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()
