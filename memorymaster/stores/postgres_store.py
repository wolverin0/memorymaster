from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from memorymaster.core.lifecycle import can_transition
from memorymaster.recall.embeddings import EmbeddingProvider, cosine_similarity
from memorymaster.core.models import (
    ActionProposal,
    CLAIM_LINK_TYPES,
    CLAIM_STATUSES,
    MEDIA_RETRY_STATUSES,
    STATUS_TRANSITION_EVENT_TYPES,
    Citation,
    CitationInput,
    Claim,
    ClaimLink,
    EvidenceItem,
    Event,
    ExternalSource,
    MediaRetryItem,
    SourceItem,
    validate_event_payload,
    validate_event_type,
    validate_transition_event_type,
)
from memorymaster.core.retry import connect_with_retry
from memorymaster.core.security import (
    sanitize_claim_input,
    sanitize_claim_structure_input,
    sanitize_event_input,
    sanitize_persisted_text,
    validate_persisted_metadata,
)
from memorymaster.stores._storage_shared import (
    ConcurrentModificationError,
    EVENT_HASH_ALGO,
    TENANT_EVENT_HASH_ALGO,
    compute_tenant_event_hash,
    generate_top_level_human_id,
)
from memorymaster.stores.claim_identity import (
    normalize_claim_identity,
    require_unambiguous_identity_row,
)
from memorymaster.stores.postgres_policy_contract import (
    canonicalize_sql_tokens,
    expected_policy_expressions,
    expressions_match,
)
from memorymaster.stores.storage import SQLiteStore

POSTGRES_EVENTS_APPEND_ONLY_TRIGGERS = (
    "trg_events_append_only_update",
    "trg_events_append_only_delete",
)
POSTGRES_CONFIRMED_TUPLE_GUARD_TRIGGER = "trg_claims_confirmed_tuple_guard"
POSTGRES_TENANT_EVENT_HASH_ALGO = TENANT_EVENT_HASH_ALGO
POSTGRES_TENANT_POLICY_TABLES = (
    "claims",
    "citations",
    "events",
    "claim_links",
    "claim_embeddings",
    "contradiction_verdicts",
    "mcp_usage",
)
POSTGRES_TEAM_DENY_TABLES = (
    "action_proposals",
    "external_sources",
    "source_items",
    "evidence_items",
    "media_retry_queue",
    "query_cache",
    "miner_state",
    "rule_stats",
)
POSTGRES_PROTECTED_TABLES = POSTGRES_TENANT_POLICY_TABLES + POSTGRES_TEAM_DENY_TABLES
POSTGRES_AUTHORITY_GUCS = (
    "memorymaster.tenant_id",
    "memorymaster.principal",
    "memorymaster.allowed_scopes",
)
POSTGRES_COMMAND_POLICIES = {
    "SELECT": "memorymaster_tenant_select",
    "INSERT": "memorymaster_tenant_insert",
    "UPDATE": "memorymaster_tenant_update",
    "DELETE": "memorymaster_tenant_delete",
}
POSTGRES_PERMIT_POLICIES = {
    command: f"{name}_permit"
    for command, name in POSTGRES_COMMAND_POLICIES.items()
}
POSTGRES_POLICY_FIELDS = (
    "schemaname",
    "tablename",
    "policyname",
    "permissive",
    "roles",
    "cmd",
    "qual",
    "with_check",
)
POSTGRES_POLICY_MANIFEST_PREFIX = "memorymaster.rls/v1;manifest=0011;sha256="
POSTGRES_METADATA_TABLES = ("cache_meta", "schema_versions")
POSTGRES_CLAIM_IDENTITY_INDEXES = frozenset(
    {
        "idx_claims_public_idempotency_key_unique",
        "idx_claims_nonpublic_principal_idempotency_key_unique",
        "idx_claims_public_human_id_unique",
        "idx_claims_nonpublic_principal_human_id_unique",
        "idx_claims_public_confirmed_tuple_unique",
        "idx_claims_nonpublic_principal_confirmed_tuple_unique",
    }
)
POSTGRES_HUMAN_IDENTITY_INDEXES = frozenset(
    {
        "idx_claims_public_human_id_unique",
        "idx_claims_nonpublic_principal_human_id_unique",
    }
)
POSTGRES_CLAIM_OWNER_CONSTRAINT = "ck_claims_identity_visibility_owner"
POSTGRES_CLAIM_OWNER_CHECK = (
    "CHECK (visibility IN ('public', 'private', 'sensitive') "
    "AND NULLIF(BTRIM(source_agent), '') IS NOT NULL)"
)
POSTGRES_EVENT_GUARD_SOURCE = """
BEGIN
    RAISE EXCEPTION 'events table is append-only; % is not allowed', TG_OP;
END;
""".strip()
POSTGRES_SUPERSESSION_GUARD_TRIGGER = "trg_claims_supersession_boundary"
POSTGRES_SUPERSESSION_GUARD_FUNCTION = "memorymaster_claim_supersession_guard"


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class PostgresStore(SQLiteStore):
    def __init__(
        self,
        dsn: str,
        *,
        tenant_id: str | None = None,
        require_tenant: bool = False,
        principal: str | None = None,
        allowed_scopes: Iterable[str] | None = None,
    ) -> None:
        self.dsn = dsn
        self.tenant_id = (tenant_id or "").strip() or None
        self.require_tenant = bool(require_tenant)
        self.principal = (principal or "").strip() or None
        self.allowed_scopes = frozenset(
            scope.strip()
            for scope in (allowed_scopes or ())
            if scope and scope.strip()
        )
        self._psycopg: Any = None
        self._vector_table_available: bool | None = None

    def _require_team_authority(self) -> tuple[str, str, tuple[str, ...]]:
        if self.tenant_id is None:
            raise PermissionError("Postgres team mode requires a tenant context.")
        if self.principal is None:
            raise PermissionError("Postgres team mode requires an authenticated principal.")
        if not self.allowed_scopes:
            raise PermissionError("Postgres team mode requires explicit allowed scopes.")
        if any("*" in scope for scope in self.allowed_scopes):
            raise PermissionError("Postgres team scopes cannot contain wildcards.")
        return self.tenant_id, self.principal, tuple(sorted(self.allowed_scopes))

    def _validate_bound_persistence_identity(self) -> None:
        validate_persisted_metadata(
            {
                "bound_tenant_id": self.tenant_id,
                "bound_principal": self.principal,
                "bound_scope": self.allowed_scopes,
            }
        )

    @staticmethod
    def _cleanup_failed_connection(conn) -> None:
        try:
            rollback = getattr(conn, "rollback", None)
            if callable(rollback):
                rollback()
        finally:
            conn.close()

    @staticmethod
    def _row_bool(row: dict[str, object], *names: str) -> bool:
        return any(bool(row.get(name)) for name in names)

    @staticmethod
    def _canonical_catalog_sql(value: object) -> str:
        return canonicalize_sql_tokens(value)

    @staticmethod
    def _canonical_identity_sql(value: object) -> str:
        return canonicalize_sql_tokens(value, drop_parentheses=True)

    @staticmethod
    def _canonical_ddl(value: object) -> str:
        normalized = canonicalize_sql_tokens(value)
        normalized = normalized.replace("public.", "")
        return " ".join(normalized.rstrip(" ;").split())

    @staticmethod
    def _policy_roles(row: dict[str, object]) -> set[str]:
        raw = row.get("roles")
        if isinstance(raw, str):
            return {part.strip() for part in raw.strip("{}").split(",") if part.strip()}
        if isinstance(raw, (list, tuple, set, frozenset)):
            return {str(part) for part in raw}
        return set()

    @staticmethod
    def _policy_is_restrictive(row: dict[str, object]) -> bool:
        permissive = row.get("permissive")
        if isinstance(permissive, str):
            return permissive.upper() == "RESTRICTIVE"
        return row.get("polpermissive") is False

    @classmethod
    def _validate_runtime_role(cls, cur) -> None:
        cur.execute(
            """
            SELECT current_user, session_user, rolname, rolsuper, rolbypassrls,
                   rolreplication, rolcreaterole, rolcreatedb,
                   EXISTS (
                       SELECT 1 FROM pg_roles AS privileged
                       WHERE privileged.rolname <> current_user
                         AND (privileged.rolsuper OR privileged.rolbypassrls)
                         AND pg_has_role(current_user, privileged.oid, 'SET')
                   ) AS member_of_privileged_role
            FROM pg_roles WHERE rolname = current_user
            """
        )
        row = cur.fetchone()
        if not isinstance(row, dict):
            raise PermissionError("Postgres runtime role could not be verified.")
        if row.get("current_user") != row.get("session_user"):
            raise PermissionError("Postgres runtime role cannot use session impersonation.")
        if bool(row.get("rolsuper")):
            raise PermissionError("Postgres runtime role cannot be a superuser.")
        if bool(row.get("rolbypassrls")):
            raise PermissionError("Postgres runtime role cannot have BYPASSRLS.")
        if bool(row.get("rolreplication")):
            raise PermissionError("Postgres runtime role cannot have REPLICATION.")
        if bool(row.get("rolcreaterole")):
            raise PermissionError("Postgres runtime role cannot have CREATEROLE.")
        if bool(row.get("rolcreatedb")):
            raise PermissionError("Postgres runtime role cannot have CREATEDB.")
        if bool(row.get("member_of_privileged_role")):
            raise PermissionError(
                "Postgres runtime role cannot be a member of a privileged superuser/BYPASSRLS role."
            )
        cur.execute(
            "SELECT has_schema_privilege(current_user, current_schema(), 'CREATE') "
            "AS public_schema_create"
        )
        schema_row = cur.fetchone()
        if isinstance(schema_row, dict) and cls._row_bool(
            schema_row, "public_schema_create", "can_create_public"
        ):
            raise PermissionError("Postgres runtime role cannot have schema CREATE privilege.")

    @classmethod
    def _validate_runtime_tables(cls, cur) -> None:
        by_name = cls._runtime_table_catalog(cur)
        for table, row in by_name.items():
            cls._validate_runtime_table_contract(table, row)

    @classmethod
    def _runtime_table_catalog(cls, cur) -> dict[str, dict[str, object]]:
        cur.execute(
            """
            SELECT c.relname AS table_name, c.relrowsecurity, c.relforcerowsecurity,
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
        rows = cur.fetchall()
        by_name = {str(row.get("table_name") or row.get("relname")): row for row in rows}
        if set(by_name) != set(POSTGRES_PROTECTED_TABLES):
            raise PermissionError("Postgres runtime requires all 15 protected tables.")
        return by_name

    @classmethod
    def _validate_runtime_table_contract(
        cls,
        table: str,
        row: dict[str, object],
    ) -> None:
        if not bool(row.get("relrowsecurity")) or not bool(row.get("relforcerowsecurity")):
            raise PermissionError(f"Postgres table {table} must ENABLE and FORCE RLS.")
        if cls._row_bool(row, "owner_member", "is_owner_member"):
            raise PermissionError(f"Postgres runtime role cannot own {table} or its owner role.")
        for privilege in ("truncate", "references", "trigger"):
            if cls._row_bool(row, f"can_{privilege}", f"has_{privilege}"):
                raise PermissionError(
                    f"Postgres runtime role cannot have {privilege.upper()} on {table}."
                )
        if table == "events":
            cls._validate_event_table_privileges(row)
        if table in POSTGRES_TEAM_DENY_TABLES:
            cls._validate_team_deny_table_privileges(table, row)

    @classmethod
    def _validate_event_table_privileges(cls, row: dict[str, object]) -> None:
        for privilege in ("select", "insert"):
            if not cls._row_bool(row, f"can_{privilege}", f"has_{privilege}"):
                raise PermissionError(
                    f"Postgres runtime role requires {privilege.upper()} "
                    "on append-only events."
                )
        if cls._row_bool(row, "can_update_any_column", "has_update_any_column"):
            raise PermissionError(
                "Postgres runtime role cannot UPDATE append-only event columns."
            )
        for privilege in ("update", "delete"):
            if cls._row_bool(row, f"can_{privilege}", f"has_{privilege}"):
                raise PermissionError(
                    f"Postgres runtime role cannot {privilege.upper()} append-only events."
                )

    @classmethod
    def _validate_team_deny_table_privileges(
        cls,
        table: str,
        row: dict[str, object],
    ) -> None:
        for privilege in ("insert", "update", "delete"):
            if cls._row_bool(row, f"can_{privilege}", f"has_{privilege}"):
                raise PermissionError(
                    f"Postgres runtime role cannot have {privilege.upper()} on "
                    f"team-deny table {table}."
                )

    @classmethod
    def _validate_runtime_metadata_tables(cls, cur) -> None:
        cur.execute(
            """
            SELECT c.relname AS table_name,
                   has_table_privilege(current_user, c.oid, 'SELECT') AS can_select,
                   has_table_privilege(current_user, c.oid, 'INSERT') AS can_insert,
                   has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
                   has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete,
                   has_table_privilege(current_user, c.oid, 'TRUNCATE') AS can_truncate,
                   has_table_privilege(current_user, c.oid, 'REFERENCES') AS can_references,
                   has_table_privilege(current_user, c.oid, 'TRIGGER') AS can_trigger
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relname = ANY(%s)
            """,
            (list(POSTGRES_METADATA_TABLES),),
        )
        rows = cur.fetchall()
        by_name = {str(row.get("table_name") or row.get("relname")): row for row in rows}
        if set(by_name) != set(POSTGRES_METADATA_TABLES):
            raise PermissionError("Postgres runtime requires both metadata tables.")
        for table, row in by_name.items():
            if not cls._row_bool(row, "can_select", "has_select"):
                raise PermissionError(f"Postgres runtime requires SELECT on {table}.")
            for privilege in ("insert", "update", "delete", "truncate", "references", "trigger"):
                if cls._row_bool(row, f"can_{privilege}", f"has_{privilege}"):
                    raise PermissionError(
                        f"Postgres runtime role cannot have {privilege.upper()} on {table}."
                    )

    @classmethod
    def _validate_confirmed_tuple_index(cls, cur) -> None:
        """Compatibility alias for the v12 six-index identity validator."""
        cls._validate_claim_identity_indexes(cur)

    @classmethod
    def _validate_claim_owner_constraint(cls, cur) -> None:
        cur.execute(
            """
            SELECT n.nspname AS schema_name, t.relname AS table_name,
                   c.conname AS constraint_name, c.contype AS constraint_type,
                   c.convalidated AS validated, c.conislocal AS is_local,
                   c.connoinherit AS no_inherit,
                   pg_get_constraintdef(c.oid, true) AS constraint_definition
            FROM pg_constraint AS c
            JOIN pg_class AS t ON t.oid = c.conrelid
            JOIN pg_namespace AS n ON n.oid = t.relnamespace
            WHERE n.nspname = current_schema() AND t.relname = 'claims'
              AND c.conname = %s
            """,
            (POSTGRES_CLAIM_OWNER_CONSTRAINT,),
        )
        row = cur.fetchone()
        if not isinstance(row, dict):
            raise PermissionError("Postgres claim owner constraint is missing.")
        metadata = (
            row.get("schema_name") == "public",
            row.get("table_name") == "claims",
            (row.get("constraint_name") or row.get("conname"))
            == POSTGRES_CLAIM_OWNER_CONSTRAINT,
            (row.get("constraint_type") or row.get("contype")) == "c",
            cls._row_bool(row, "validated", "convalidated"),
            cls._row_bool(row, "is_local", "conislocal"),
            not cls._row_bool(row, "no_inherit", "connoinherit"),
        )
        definition = row.get("constraint_definition") or row.get("definition")
        if not all(metadata) or not expressions_match(
            definition,
            POSTGRES_CLAIM_OWNER_CHECK,
        ):
            raise PermissionError(
                "Postgres claim owner constraint is unsafe or not validated."
            )

    @classmethod
    def _expected_claim_identity_catalog(cls) -> dict[str, tuple[str, str]]:
        identities = {
            "idempotency_key": ("scope, idempotency_key", "idempotency_key IS NOT NULL"),
            "human_id": ("scope, human_id", "human_id IS NOT NULL"),
            "confirmed_tuple": (
                "subject, predicate, scope",
                "status = 'confirmed'::text AND subject IS NOT NULL "
                "AND predicate IS NOT NULL",
            ),
        }
        expected: dict[str, tuple[str, str]] = {}
        for suffix, (columns, required) in identities.items():
            for namespace in ("public", "nonpublic_principal"):
                name = f"idx_claims_{namespace}_{suffix}_unique"
                public = namespace == "public"
                keys = f"COALESCE(tenant_id, ''::text), {columns}"
                predicate = f"visibility = 'public'::text AND {required}"
                if not public:
                    if suffix == "confirmed_tuple":
                        keys = (
                            "COALESCE(tenant_id, ''::text), visibility, source_agent, "
                            f"{columns}"
                        )
                    else:
                        keys = (
                            "COALESCE(tenant_id, ''::text), scope, visibility, "
                            f"source_agent, {columns.removeprefix('scope, ')}"
                        )
                    predicate = (
                        "visibility <> 'public'::text AND source_agent IS NOT NULL "
                        f"AND {required}"
                    )
                definition = (
                    f"CREATE UNIQUE INDEX {name} ON public.claims USING btree ({keys}) "
                    f"WHERE ({predicate})"
                )
                expected[name] = (definition, predicate)
        return expected

    @classmethod
    def _validate_claim_identity_indexes(cls, cur) -> None:
        cur.execute(
            """
            SELECT i.relname AS index_name, x.indisunique, x.indisvalid, x.indisready,
                   pg_get_indexdef(i.oid) AS indexdef,
                   pg_get_expr(x.indpred, x.indrelid, false) AS predicate
            FROM pg_index AS x
            JOIN pg_class AS i ON i.oid = x.indexrelid
            JOIN pg_class AS t ON t.oid = x.indrelid
            JOIN pg_namespace AS n ON n.oid = t.relnamespace
            WHERE n.nspname = current_schema() AND t.relname = 'claims'
              AND x.indisunique AND NOT x.indisprimary
            """
        )
        rows = list(cur.fetchall())
        by_name = {
            str(row.get("index_name") or row.get("relname")): row for row in rows
        }
        expected = cls._expected_claim_identity_catalog()
        if set(by_name) != set(expected) or len(rows) != len(expected):
            raise PermissionError("Postgres claim identity index catalog is unsafe.")
        for name, (definition, predicate) in expected.items():
            row = by_name[name]
            flags = (("indisunique", "is_unique"), ("indisvalid", "is_valid"),
                     ("indisready", "is_ready"))
            if not all(cls._row_bool(row, primary, alias) for primary, alias in flags):
                raise PermissionError(f"Postgres claim identity index {name} is unsafe.")
            actual_definition = row.get("indexdef") or row.get("index_definition")
            actual_predicate = row.get("predicate") or row.get("index_predicate")
            if cls._canonical_identity_sql(actual_definition) != cls._canonical_identity_sql(
                definition
            ) or cls._canonical_identity_sql(actual_predicate) != cls._canonical_identity_sql(
                predicate
            ):
                raise PermissionError(f"Postgres claim identity index {name} has drifted.")

    @classmethod
    def _validate_event_chain_head_function(cls, cur) -> None:
        cur.execute(
            """
            SELECT n.nspname AS schema_name, p.proname AS function_name,
                   p.pronargs AS argument_count,
                   pg_get_function_result(p.oid) AS result_signature,
                   l.lanname AS language_name, p.prosecdef AS security_definer,
                   COALESCE(p.proconfig, ARRAY[]::text[]) AS function_config,
                   p.provolatile AS volatility, p.proparallel AS parallel_safety,
                   p.proleakproof AS leakproof, p.proisstrict AS strict,
                   p.prosrc AS function_source,
                   EXISTS (
                       SELECT 1
                       FROM aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) AS acl
                       WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                   ) AS public_execute,
                   has_function_privilege(current_user, p.oid, 'EXECUTE') AS runtime_execute,
                    p.proowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
                        AS owner_is_runtime,
                    pg_has_role(current_user, p.proowner, 'MEMBER') AS owner_member,
                    owner_role.rolsuper AS owner_superuser,
                    owner_role.rolbypassrls AS owner_bypassrls,
                    pg_get_functiondef(p.oid) AS function_definition
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            JOIN pg_language AS l ON l.oid = p.prolang
            JOIN pg_roles AS owner_role ON owner_role.oid = p.proowner
            WHERE n.nspname = 'public'
              AND p.proname = 'memorymaster_event_chain_head'
              AND p.pronargs = 0
            """
        )
        row = cur.fetchone()
        if not isinstance(row, dict):
            raise PermissionError("Postgres event-chain head function is missing.")
        cls._validate_event_chain_head_metadata(row)

    @classmethod
    def _validate_event_chain_head_metadata(cls, row: dict[str, object]) -> None:
        if row.get("schema_name") != "public" or row.get("function_name") != (
            "memorymaster_event_chain_head"
        ):
            raise PermissionError("Postgres event-chain head function signature is unsafe.")
        argument_count = row.get("argument_count")
        if argument_count is None or int(argument_count) != 0:
            raise PermissionError("Postgres event-chain head function argument signature is unsafe.")
        result = cls._canonical_identity_sql(row.get("result_signature"))
        if result != "table global_event_hash text, tenant_event_hash text":
            raise PermissionError("Postgres event-chain head result signature is unsafe.")
        if str(row.get("language_name") or "").lower() != "plpgsql":
            raise PermissionError("Postgres event-chain head language is unsafe.")
        if not bool(row.get("security_definer")):
            raise PermissionError("Postgres event-chain head function must be SECURITY DEFINER.")
        configs = row.get("function_config") or ()
        if isinstance(configs, str):
            configs = (configs,)
        normalized_configs = {cls._canonical_catalog_sql(value) for value in configs}
        if normalized_configs != {"search_path=pg_catalog, pg_temp"}:
            raise PermissionError("Postgres event-chain head function has an unsafe search_path.")
        if (
            row.get("volatility") != "v"
            or row.get("parallel_safety") != "u"
            or bool(row.get("leakproof"))
            or bool(row.get("strict"))
        ):
            raise PermissionError("Postgres event-chain head function catalog has drifted.")
        if bool(row.get("public_execute")) or not bool(row.get("runtime_execute")):
            raise PermissionError("Postgres event-chain head EXECUTE privileges are unsafe.")
        cls._validate_event_chain_head_owner(row)
        import importlib

        migration = importlib.import_module(
            "memorymaster.stores.migrations.0011_postgres_scoped_force_rls"
        )
        expected_source = str(migration._EVENT_HEAD_FUNCTION).split(
            "AS $$", 1
        )[1].rsplit("$$", 1)[0].strip()
        if cls._canonical_catalog_sql(row.get("function_source")) != (
            cls._canonical_catalog_sql(expected_source)
        ):
            raise PermissionError("Postgres event-chain head function body has drifted.")

    @staticmethod
    def _validate_event_chain_head_owner(row: dict[str, object]) -> None:
        if bool(row.get("owner_is_runtime")) or bool(row.get("owner_member")):
            raise PermissionError("Postgres runtime role cannot own the event-chain head function.")
        if not (
            bool(row.get("owner_superuser"))
            or bool(row.get("owner_bypassrls"))
        ):
            raise PermissionError(
                "Postgres event-chain head owner must be SUPERUSER or BYPASSRLS."
            )

    @classmethod
    def _validate_event_append_only_catalog(cls, cur) -> None:
        cur.execute(
            """
            SELECT tg.tgname AS trigger_name, ns.nspname AS table_schema,
                   tbl.relname AS table_name, tg.tgenabled AS enabled_code,
                   tg.tgisinternal AS is_internal, fns.nspname AS function_schema,
                   fn.proname AS function_name,
                   pg_get_triggerdef(tg.oid, true) AS trigger_definition
            FROM pg_trigger AS tg
            JOIN pg_class AS tbl ON tbl.oid = tg.tgrelid
            JOIN pg_namespace AS ns ON ns.oid = tbl.relnamespace
            JOIN pg_proc AS fn ON fn.oid = tg.tgfoid
            JOIN pg_namespace AS fns ON fns.oid = fn.pronamespace
            WHERE ns.nspname = 'public' AND tbl.relname = 'events'
              AND NOT tg.tgisinternal
            """,
        )
        rows = list(cur.fetchall())
        by_name = {str(row.get("trigger_name")): row for row in rows}
        if (
            set(by_name) != set(POSTGRES_EVENTS_APPEND_ONLY_TRIGGERS)
            or len(rows) != len(POSTGRES_EVENTS_APPEND_ONLY_TRIGGERS)
        ):
            raise PermissionError("Postgres append-only event trigger catalog is unsafe.")
        for operation in ("update", "delete"):
            cls._validate_event_trigger_row(
                by_name[f"trg_events_append_only_{operation}"],
                operation,
            )
        cls._validate_event_guard_function(cur)

    @classmethod
    def _validate_claim_supersession_guard(cls, cur) -> None:
        cur.execute(
            """
            SELECT tg.tgname AS trigger_name, ns.nspname AS table_schema,
                   tbl.relname AS table_name, tg.tgenabled AS enabled_code,
                   tg.tgisinternal AS is_internal, fns.nspname AS function_schema,
                   fn.proname AS function_name,
                   pg_get_triggerdef(tg.oid, true) AS trigger_definition
            FROM pg_trigger AS tg
            JOIN pg_class AS tbl ON tbl.oid = tg.tgrelid
            JOIN pg_namespace AS ns ON ns.oid = tbl.relnamespace
            JOIN pg_proc AS fn ON fn.oid = tg.tgfoid
            JOIN pg_namespace AS fns ON fns.oid = fn.pronamespace
            WHERE ns.nspname = 'public' AND tbl.relname = 'claims'
              AND NOT tg.tgisinternal
            """,
        )
        rows = list(cur.fetchall())
        by_name = {str(row.get("trigger_name")): row for row in rows}
        if set(by_name) != {POSTGRES_SUPERSESSION_GUARD_TRIGGER} or len(rows) != 1:
            raise PermissionError(
                "Postgres claim supersession trigger catalog is unsafe."
            )
        row = by_name[POSTGRES_SUPERSESSION_GUARD_TRIGGER]
        cls._validate_supersession_trigger_row(row)
        cls._validate_supersession_guard_function(cur)

    @classmethod
    def _validate_supersession_trigger_row(cls, row: dict[str, object]) -> None:
        expected = (
            f"CREATE TRIGGER {POSTGRES_SUPERSESSION_GUARD_TRIGGER} BEFORE INSERT OR "
            "UPDATE OF tenant_id, scope, visibility, source_agent, "
            "supersedes_claim_id, replaced_by_claim_id ON public.claims "
            "FOR EACH ROW EXECUTE FUNCTION "
            f"public.{POSTGRES_SUPERSESSION_GUARD_FUNCTION}()"
        )
        metadata = (
            row.get("trigger_name") == POSTGRES_SUPERSESSION_GUARD_TRIGGER,
            row.get("table_schema") == "public",
            row.get("table_name") == "claims",
            row.get("enabled_code") == "O",
            not bool(row.get("is_internal")),
            row.get("function_schema") == "public",
            row.get("function_name") == POSTGRES_SUPERSESSION_GUARD_FUNCTION,
        )
        if not all(metadata) or cls._canonical_ddl(
            row.get("trigger_definition")
        ) != cls._canonical_ddl(expected):
            raise PermissionError("Postgres claim supersession trigger has drifted.")

    @classmethod
    def _validate_supersession_guard_function(cls, cur) -> None:
        cur.execute(
            """
            SELECT n.nspname AS schema_name, p.proname AS function_name,
                   p.pronargs AS argument_count,
                   pg_get_function_result(p.oid) AS result_signature,
                   l.lanname AS language_name, p.prosecdef AS security_definer,
                   COALESCE(p.proconfig, ARRAY[]::text[]) AS function_config,
                   p.provolatile AS volatility, p.proparallel AS parallel_safety,
                   p.proleakproof AS leakproof, p.proisstrict AS strict,
                   p.prosrc AS function_source,
                   pg_has_role(current_user, p.proowner, 'MEMBER') AS owner_member
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            JOIN pg_language AS l ON l.oid = p.prolang
            WHERE n.nspname = 'public' AND p.proname = %s AND p.pronargs = 0
            """,
            (POSTGRES_SUPERSESSION_GUARD_FUNCTION,),
        )
        row = cur.fetchone()
        if not isinstance(row, dict):
            raise PermissionError("Postgres claim supersession guard function is missing.")
        if not cls._supersession_guard_metadata_matches(row):
            raise PermissionError("Postgres claim supersession guard has drifted.")

    @classmethod
    def _supersession_guard_metadata_matches(
        cls,
        row: dict[str, object],
    ) -> bool:
        import importlib

        migration = importlib.import_module(
            "memorymaster.stores.migrations.0012_principal_local_claim_identities"
        )
        expected_source = str(migration._SUPERSESSION_GUARD_FUNCTION).split(
            "AS $$", 1
        )[1].rsplit("$$", 1)[0].strip()
        configs = row.get("function_config") or ()
        if isinstance(configs, str):
            configs = (configs,)
        metadata = (
            row.get("schema_name") == "public",
            row.get("function_name") == POSTGRES_SUPERSESSION_GUARD_FUNCTION,
            row.get("argument_count") is not None
            and int(row["argument_count"]) == 0,
            cls._canonical_identity_sql(row.get("result_signature")) == "trigger",
            str(row.get("language_name") or "").lower() == "plpgsql",
            not bool(row.get("security_definer")),
            not tuple(configs),
            row.get("volatility") == "v",
            row.get("parallel_safety") == "u",
            not bool(row.get("leakproof")),
            not bool(row.get("strict")),
            not bool(row.get("owner_member")),
            cls._canonical_catalog_sql(row.get("function_source"))
            == cls._canonical_catalog_sql(expected_source),
        )
        return all(metadata)

    @classmethod
    def _validate_event_trigger_row(
        cls,
        row: dict[str, object],
        operation: str,
    ) -> None:
        name = f"trg_events_append_only_{operation}"
        expected = (
            f"CREATE TRIGGER {name} BEFORE {operation.upper()} ON public.events "
            "FOR EACH ROW EXECUTE FUNCTION "
            "public.memorymaster_events_append_only_guard()"
        )
        metadata = (
            row.get("trigger_name") == name,
            row.get("table_schema") == "public",
            row.get("table_name") == "events",
            row.get("enabled_code") == "O",
            not bool(row.get("is_internal")),
            row.get("function_schema") == "public",
            row.get("function_name") == "memorymaster_events_append_only_guard",
        )
        if not all(metadata) or cls._canonical_ddl(
            row.get("trigger_definition")
        ) != cls._canonical_ddl(expected):
            raise PermissionError(f"Postgres append-only {operation} trigger has drifted.")

    @classmethod
    def _validate_event_guard_function(cls, cur) -> None:
        cur.execute(
            """
            SELECT n.nspname AS schema_name, p.proname AS function_name,
                   p.pronargs AS argument_count,
                   pg_get_function_result(p.oid) AS result_signature,
                   l.lanname AS language_name, p.prosecdef AS security_definer,
                   COALESCE(p.proconfig, ARRAY[]::text[]) AS function_config,
                   p.provolatile AS volatility, p.proparallel AS parallel_safety,
                   p.proleakproof AS leakproof, p.proisstrict AS strict,
                   p.prosrc AS function_source,
                   pg_has_role(current_user, p.proowner, 'MEMBER') AS owner_member
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            JOIN pg_language AS l ON l.oid = p.prolang
            WHERE n.nspname = 'public'
              AND p.proname = 'memorymaster_events_append_only_guard'
              AND p.pronargs = 0
            """
        )
        row = cur.fetchone()
        if not isinstance(row, dict):
            raise PermissionError("Postgres append-only event guard is missing.")
        configs = row.get("function_config") or ()
        if isinstance(configs, str):
            configs = (configs,)
        metadata = (
            row.get("schema_name") == "public",
            row.get("function_name") == "memorymaster_events_append_only_guard",
            row.get("argument_count") is not None
            and int(row["argument_count"]) == 0,
            cls._canonical_identity_sql(row.get("result_signature")) == "trigger",
            str(row.get("language_name") or "").lower() == "plpgsql",
            not bool(row.get("security_definer")),
            not tuple(configs),
            row.get("volatility") == "v",
            row.get("parallel_safety") == "u",
            not bool(row.get("leakproof")),
            not bool(row.get("strict")),
            not bool(row.get("owner_member")),
            cls._canonical_catalog_sql(row.get("function_source"))
            == cls._canonical_catalog_sql(POSTGRES_EVENT_GUARD_SOURCE),
        )
        if not all(metadata):
            raise PermissionError("Postgres append-only event guard has drifted.")

    @staticmethod
    def _canonical_policy_payload(rows: Iterable[dict[str, object]]) -> str:
        payload: list[dict[str, object]] = []
        for policy in rows:
            row = {field: policy.get(field) for field in POSTGRES_POLICY_FIELDS}
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

    @classmethod
    def _policy_manifest_comment(cls, rows: Iterable[dict[str, object]]) -> str:
        payload = cls._canonical_policy_payload(rows).encode("utf-8")
        return f"{POSTGRES_POLICY_MANIFEST_PREFIX}{hashlib.sha256(payload).hexdigest()}"

    @staticmethod
    def _expected_policy_inventory() -> dict[tuple[str, str], tuple[bool, str]]:
        expected: dict[tuple[str, str], tuple[bool, str]] = {}
        for table in POSTGRES_TENANT_POLICY_TABLES:
            for command, restrict_name in POSTGRES_COMMAND_POLICIES.items():
                expected[(table, restrict_name)] = (True, command)
                expected[(table, POSTGRES_PERMIT_POLICIES[command])] = (False, command)
        for table in POSTGRES_TEAM_DENY_TABLES:
            expected[(table, "memorymaster_team_deny")] = (True, "ALL")
        return expected

    @classmethod
    def _validate_policy_shape(
        cls,
        row: dict[str, object],
        expected: tuple[bool, str],
    ) -> None:
        restrictive, command = expected
        if cls._policy_is_restrictive(row) is not restrictive:
            raise PermissionError("Postgres runtime RLS policy mode is unsafe.")
        if str(row.get("cmd", "")).upper() != command:
            raise PermissionError("Postgres runtime RLS policy command is unsafe.")
        if cls._policy_roles(row) != {"public"}:
            raise PermissionError("Postgres runtime RLS policy roles are unsafe.")
        qual_present = row.get("qual") is not None
        check_present = row.get("with_check") is not None
        expected_shape = {
            "SELECT": (True, False),
            "INSERT": (False, True),
            "UPDATE": (True, True),
            "DELETE": (True, False),
            "ALL": (True, True),
        }[command]
        if (qual_present, check_present) != expected_shape:
            raise PermissionError("Postgres runtime RLS policy expression shape is unsafe.")

    @staticmethod
    def _validate_paired_policy_expressions(
        policies: dict[tuple[str, str], dict[str, object]],
    ) -> None:
        for table in POSTGRES_TENANT_POLICY_TABLES:
            for command, restrict_name in POSTGRES_COMMAND_POLICIES.items():
                permit = policies[(table, POSTGRES_PERMIT_POLICIES[command])]
                restrict = policies[(table, restrict_name)]
                if (permit.get("qual"), permit.get("with_check")) != (
                    restrict.get("qual"),
                    restrict.get("with_check"),
                ):
                    raise PermissionError("Postgres paired RLS policy expressions differ.")

    @staticmethod
    def _validate_policy_expression_contract(
        policies: dict[tuple[str, str], dict[str, object]],
    ) -> None:
        expected = expected_policy_expressions(
            POSTGRES_TENANT_POLICY_TABLES,
            POSTGRES_TEAM_DENY_TABLES,
            POSTGRES_COMMAND_POLICIES,
            POSTGRES_PERMIT_POLICIES,
        )
        for identity, (expected_qual, expected_check) in expected.items():
            policy = policies[identity]
            if not expressions_match(policy.get("qual"), expected_qual) or not (
                expressions_match(policy.get("with_check"), expected_check)
            ):
                raise PermissionError(
                    f"Postgres RLS policy expression contract drifted: "
                    f"{identity[0]}.{identity[1]}."
                )

    @staticmethod
    def _validate_runtime_migration(cur) -> None:
        from memorymaster.stores.migrations import discover_migrations

        required_versions = (11, 12)
        migrations = {
            item.version: item
            for item in discover_migrations()
            if item.version in required_versions
        }
        cur.execute(
            """
            SELECT version, checksum FROM schema_versions
            WHERE version IN (%s, %s)
            """,
            required_versions,
        )
        rows = list(cur.fetchall())
        stored = {
            int(row["version"]): str(row["checksum"])
            for row in rows
            if isinstance(row, dict)
        }
        expected = {version: migrations[version].checksum() for version in required_versions}
        if stored != expected:
            raise PermissionError("Postgres runtime migration checksums are missing or invalid.")

    @classmethod
    def _validate_runtime_policies(cls, cur) -> None:
        cur.execute(
            """
            SELECT schemaname, tablename, policyname, permissive, roles, cmd, qual, with_check
            FROM pg_policies
            WHERE schemaname = current_schema() AND tablename = ANY(%s)
            """,
            (list(POSTGRES_PROTECTED_TABLES),),
        )
        rows = list(cur.fetchall())
        policies = {
            (str(row.get("tablename") or row.get("table_name")),
             str(row.get("policyname") or row.get("policy_name"))): row
            for row in rows
        }
        expected = cls._expected_policy_inventory()
        if set(policies) != set(expected) or len(rows) != len(expected):
            raise PermissionError("Postgres runtime RLS policy inventory is unsafe.")
        for identity, contract in expected.items():
            cls._validate_policy_shape(policies[identity], contract)
        cls._validate_paired_policy_expressions(policies)
        cls._validate_policy_expression_contract(policies)
        for table in POSTGRES_TEAM_DENY_TABLES:
            deny = policies[(table, "memorymaster_team_deny")]
            if str(deny.get("qual")).upper() != "FALSE" or str(
                deny.get("with_check")
            ).upper() != "FALSE":
                raise PermissionError("Postgres team-deny RLS policy must remain FALSE.")
        cur.execute(
            """
            SELECT obj_description(p.oid, 'pg_policy') AS manifest_comment
            FROM pg_policy AS p
            JOIN pg_class AS c ON c.oid = p.polrelid
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname = 'claims'
              AND p.polname = 'memorymaster_tenant_select'
            """
        )
        comment_row = cur.fetchone()
        comment = comment_row.get("manifest_comment") if isinstance(comment_row, dict) else None
        if comment != cls._policy_manifest_comment(rows):
            raise PermissionError("Postgres RLS policy manifest fingerprint is invalid.")

    @staticmethod
    def _authority_settings(cur) -> dict[str, str]:
        cur.execute(
            """
            SELECT current_setting('memorymaster.tenant_id', true) AS tenant_id,
                   current_setting('memorymaster.principal', true) AS principal,
                   current_setting('memorymaster.allowed_scopes', true) AS allowed_scopes
            """
        )
        row = cur.fetchone() or {}
        return {name: str(row.get(name) or "") for name in ("tenant_id", "principal", "allowed_scopes")}

    @classmethod
    def _bind_runtime_authority(
        cls,
        cur,
        tenant_id: str,
        principal: str,
        allowed_scopes: tuple[str, ...],
    ) -> None:
        if any(cls._authority_settings(cur).values()):
            raise PermissionError("Postgres authority GUC defaults must be empty.")
        values = {
            "memorymaster.tenant_id": tenant_id,
            "memorymaster.principal": principal,
            "memorymaster.allowed_scopes": json.dumps(allowed_scopes, separators=(",", ":")),
        }
        for key, value in values.items():
            cur.execute("SELECT set_config(%s, %s, true)", (key, value))
        if cls._authority_settings(cur) != {
            "tenant_id": tenant_id,
            "principal": principal,
            "allowed_scopes": values["memorymaster.allowed_scopes"],
        }:
            raise PermissionError("Postgres transaction-local authority binding failed verification.")

    def _tenant_for_operation(self, tenant_id: str | None = None) -> str | None:
        requested = (tenant_id or "").strip() or None
        if self.require_tenant:
            if self.tenant_id is None:
                raise PermissionError("Postgres team mode requires a tenant context.")
            if requested is not None and requested != self.tenant_id:
                raise PermissionError("Caller tenant does not match the bound tenant context.")
            return self.tenant_id
        return requested if requested is not None else self.tenant_id

    @staticmethod
    def _postgres_identity_filter(
        visibility: str,
        source_agent: str | None,
        *,
        alias: str = "",
    ) -> tuple[str, tuple[object, ...]]:
        prefix = f"{alias}." if alias else ""
        if visibility == "public":
            return f"{prefix}visibility = %s", ("public",)
        return (
            f"{prefix}visibility = %s AND {prefix}source_agent = %s",
            (visibility, source_agent),
        )

    def _load_psycopg(self) -> Any:
        if self._psycopg is None:
            try:
                import psycopg  # type: ignore
                from psycopg.rows import dict_row  # type: ignore
                from psycopg.types.json import Jsonb  # type: ignore
            except Exception as exc:  # pragma: no cover
                raise RuntimeError(
                    "Postgres backend requires psycopg. Install with: pip install 'memorymaster[postgres]'"
                ) from exc
            self._psycopg = (psycopg, dict_row, Jsonb)
        return self._psycopg

    def _open_connection(self) -> Any:
        psycopg, dict_row, _ = self._load_psycopg()

        def _open() -> Any:
            return psycopg.connect(self.dsn, row_factory=dict_row)

        return connect_with_retry(_open)

    def connect(self) -> Any:
        if not self.require_tenant:
            raise PermissionError(
                "Postgres application connections require authenticated team authority. "
                "Use SQLite for local trusted mode or init_db() with a dedicated migrator DSN."
            )
        self._validate_bound_persistence_identity()
        authority = self._require_team_authority()
        conn = self._open_connection()
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                self._validate_runtime_role(cur)
                self._validate_runtime_tables(cur)
                self._validate_runtime_metadata_tables(cur)
                self._validate_claim_owner_constraint(cur)
                self._validate_claim_identity_indexes(cur)
                self._validate_claim_supersession_guard(cur)
                self._validate_event_append_only_catalog(cur)
                self._validate_event_chain_head_function(cur)
                self._validate_runtime_migration(cur)
                self._validate_runtime_policies(cur)
                self._bind_runtime_authority(cur, *authority)
        except Exception:
            self._cleanup_failed_connection(conn)
            raise
        return conn

    def connect_ro(self) -> Any:
        raise PermissionError(
            "connect_ro is a SQLite-only surface and is unavailable in Postgres team mode."
        )

    def _deny_unsupported_team_surface(self, surface: str) -> None:
        raise PermissionError(
            f"{surface} is unavailable in Postgres team mode until its tables "
            "have tenant-scoped policy coverage."
        )

    def _connect_schema_admin(self) -> Any:
        if self.require_tenant:
            raise PermissionError(
                "Postgres team runtime stores cannot open schema-administration connections."
            )
        conn = self._open_connection()
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT current_user, session_user, rolsuper, rolbypassrls
                    FROM pg_roles WHERE rolname = current_user
                    """
                )
                row = cur.fetchone()
            if not isinstance(row, dict):
                raise PermissionError("Postgres migration role could not be verified.")
            if row.get("current_user") != row.get("session_user"):
                raise PermissionError("Postgres migration role cannot use session impersonation.")
            if not bool(row.get("rolsuper")) and not bool(row.get("rolbypassrls")):
                raise PermissionError(
                    "Postgres migration role requires SUPERUSER or BYPASSRLS."
                )
        except Exception:
            self._cleanup_failed_connection(conn)
            raise
        return conn

    def init_db(self) -> None:
        if self.require_tenant:
            self._require_team_authority()
            raise PermissionError(
                "Postgres team runtime stores cannot initialize or migrate schema."
            )
        with self._connect_schema_admin() as conn, conn.cursor() as cur:
            from memorymaster.stores._storage_schema import load_schema_postgres_sql

            sql = load_schema_postgres_sql()
            statements = self._split_sql_statements(sql)
            for statement in statements:
                cur.execute(statement)
            self._ensure_confirmed_tuple_uniqueness_schema(conn)
            self._ensure_event_integrity_schema(conn)
            self._ensure_claim_links_schema(conn)
            self._ensure_human_id_schema(conn)
            self._ensure_tenant_id_schema(conn)
            self._ensure_binding_schema(conn)

        # v3.20.0-S1: apply versioned migrations after the legacy init flow.
        # The 0001 baseline is a no-op for existing schemas; future
        # migrations (0002+) apply on top via the same runner that drives
        # the SQLite backend, ensuring parity between the two stores.
        from memorymaster.stores.migrations import MigrationRunner

        with self._connect_schema_admin() as mig_conn:
            MigrationRunner(mig_conn, backend="postgres").apply_pending()

    @staticmethod
    def _canonical_payload(payload: object | None) -> str:
        if payload is None:
            return ""
        if isinstance(payload, str):
            raw = payload.strip()
            if not raw:
                return ""
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _compute_event_hash(
        *,
        claim_id: int | None,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        details: str | None,
        payload: object | None,
        created_at: datetime,
        prev_event_hash: str | None,
        tenant_id: str | None = None,
        hash_algo: str = EVENT_HASH_ALGO,
        canonicalize_timestamp: bool = True,
    ) -> str:
        normalized_created_at = created_at
        if canonicalize_timestamp and created_at.tzinfo is not None:
            normalized_created_at = created_at.astimezone(timezone.utc)
        created_iso = normalized_created_at.replace(microsecond=0).isoformat()
        components = [hash_algo]
        if hash_algo == POSTGRES_TENANT_EVENT_HASH_ALGO:
            if tenant_id is None:
                raise ValueError("Tenant event hashes require tenant_id.")
            components.append(tenant_id)
        components.extend([
            str(claim_id) if claim_id is not None else "",
            event_type,
            from_status or "",
            to_status or "",
            details or "",
            PostgresStore._canonical_payload(payload),
            created_iso,
            prev_event_hash or "",
        ])
        material = "\x1f".join(components)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _compute_tenant_event_hash(
        *,
        tenant_id: str,
        event_hash: str,
        tenant_prev_event_hash: str | None,
    ) -> str:
        return compute_tenant_event_hash(
            tenant_id=tenant_id,
            event_hash=event_hash,
            tenant_prev_event_hash=tenant_prev_event_hash,
        )

    @staticmethod
    def _ensure_event_integrity_schema(conn) -> None:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS prev_event_hash TEXT")
            cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS event_hash TEXT")
            cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS hash_algo TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_event_hash ON events(event_hash)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_prev_event_hash ON events(prev_event_hash)")
            PostgresStore._drop_events_append_only_triggers(cur)
            PostgresStore._backfill_event_chain(conn)
            PostgresStore._ensure_events_append_only_rules(cur)

    @staticmethod
    def _ensure_confirmed_tuple_uniqueness_schema(conn) -> None:
        PostgresStore._ensure_tenant_id_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"DROP TRIGGER IF EXISTS {POSTGRES_CONFIRMED_TUPLE_GUARD_TRIGGER} ON claims"
            )
            cur.execute(
                "DROP FUNCTION IF EXISTS memorymaster_claims_confirmed_tuple_guard()"
            )
            PostgresStore._try_create_confirmed_tuple_unique_index(cur)

    @staticmethod
    def _try_create_confirmed_tuple_unique_index(cur) -> None:
        savepoint = "sp_claims_confirmed_tuple_unique_idx"
        cur.execute(f"SAVEPOINT {savepoint}")
        try:
            cur.execute("DROP INDEX IF EXISTS idx_claims_confirmed_tuple_unique")
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_public_confirmed_tuple_unique
                    ON claims(COALESCE(tenant_id, ''), subject, predicate, scope)
                    WHERE visibility = 'public' AND status = 'confirmed'
                      AND subject IS NOT NULL AND predicate IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_nonpublic_principal_confirmed_tuple_unique
                    ON claims(
                        COALESCE(tenant_id, ''), visibility, source_agent,
                        subject, predicate, scope
                    )
                    WHERE visibility <> 'public' AND source_agent IS NOT NULL
                      AND status = 'confirmed'
                      AND subject IS NOT NULL AND predicate IS NOT NULL
                """
            )
        except Exception as exc:
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
            lowered = str(exc).lower()
            if (
                "could not create unique index" in lowered
                or "duplicate key value" in lowered
                or "is duplicated" in lowered
            ):
                return
            raise
        cur.execute(f"RELEASE SAVEPOINT {savepoint}")

    @staticmethod
    def _drop_events_append_only_triggers(cur) -> None:
        for trigger in POSTGRES_EVENTS_APPEND_ONLY_TRIGGERS:
            cur.execute(f"DROP TRIGGER IF EXISTS {trigger} ON events")

    @staticmethod
    def _ensure_events_append_only_rules(cur) -> None:
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION memorymaster_events_append_only_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'events table is append-only; % is not allowed', TG_OP;
            END;
            $$;
            """
        )
        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgname = 'trg_events_append_only_update'
                      AND tgrelid = 'events'::regclass
                ) THEN
                    CREATE TRIGGER trg_events_append_only_update
                    BEFORE UPDATE ON events
                    FOR EACH ROW
                    EXECUTE FUNCTION memorymaster_events_append_only_guard();
                END IF;
            END
            $$;
            """
        )
        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgname = 'trg_events_append_only_delete'
                      AND tgrelid = 'events'::regclass
                ) THEN
                    CREATE TRIGGER trg_events_append_only_delete
                    BEFORE DELETE ON events
                    FOR EACH ROW
                    EXECUTE FUNCTION memorymaster_events_append_only_guard();
                END IF;
            END
            $$;
            """
        )

    @staticmethod
    def _primary_event_partition(
        row: dict[str, object],
        global_head: str | None,
        tenant_heads: dict[str, str | None],
    ) -> tuple[str, str | None, str | None]:
        hash_algo = PostgresStore._as_text(row.get("hash_algo")) or EVENT_HASH_ALGO
        tenant_id = PostgresStore._as_text(row.get("tenant_id"))
        if hash_algo == EVENT_HASH_ALGO:
            return hash_algo, tenant_id, global_head
        if hash_algo == POSTGRES_TENANT_EVENT_HASH_ALGO and tenant_id is not None:
            return hash_algo, tenant_id, tenant_heads.get(tenant_id)
        raise RuntimeError(f"Invalid primary event partition at event {row['id']}.")

    @staticmethod
    def _hash_primary_event_row(
        row: dict[str, object],
        *,
        previous: str | None,
        tenant_id: str | None,
        hash_algo: str,
    ) -> str:
        created_at = row["created_at"]
        if not isinstance(created_at, datetime):
            created_at = datetime.fromisoformat(str(created_at))
        return PostgresStore._compute_event_hash(
            claim_id=int(row["claim_id"]) if row["claim_id"] is not None else None,
            event_type=str(row["event_type"]),
            from_status=PostgresStore._as_text(row["from_status"]),
            to_status=PostgresStore._as_text(row["to_status"]),
            details=PostgresStore._as_text(row["details"]),
            payload=row.get("payload_json"),
            created_at=created_at,
            prev_event_hash=previous,
            tenant_id=tenant_id,
            hash_algo=hash_algo,
        )

    @staticmethod
    def _backfill_event_chain(conn, *, rebuild_all: bool = False) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event.id, event.claim_id, event.event_type, event.from_status,
                       event.to_status, event.details, event.payload_json,
                       event.created_at, event.prev_event_hash, event.event_hash,
                       event.hash_algo, to_jsonb(event)->>'tenant_id' AS tenant_id
                FROM events AS event
                ORDER BY id ASC
                """
            )
            rows = cur.fetchall()
            if not rows:
                return 0
            if rebuild_all and any(
                PostgresStore._as_text(row.get("hash_algo")) == POSTGRES_TENANT_EVENT_HASH_ALGO
                for row in rows
            ):
                raise RuntimeError("Cannot rebuild a mixed v1/tenant-v2 primary event ledger.")

            updated = 0
            global_head: str | None = None
            tenant_heads: dict[str, str | None] = {}
            for row in rows:
                row_hash = PostgresStore._as_text(row.get("event_hash"))
                row_algo, tenant_id, previous = PostgresStore._primary_event_partition(
                    row,
                    global_head,
                    tenant_heads,
                )
                if row_hash and not rebuild_all:
                    if PostgresStore._as_text(row.get("prev_event_hash")) != previous:
                        raise RuntimeError(f"Invalid primary event predecessor at event {row['id']}.")
                    event_hash = row_hash
                else:
                    event_hash = PostgresStore._hash_primary_event_row(
                        row,
                        previous=previous,
                        tenant_id=tenant_id,
                        hash_algo=row_algo,
                    )
                    cur.execute(
                        "UPDATE events SET prev_event_hash = %s, event_hash = %s, hash_algo = %s WHERE id = %s",
                        (previous, event_hash, row_algo, int(row["id"])),
                    )
                    updated += 1
                if row_algo == EVENT_HASH_ALGO:
                    global_head = event_hash
                else:
                    tenant_heads[tenant_id] = event_hash
            return updated

    @staticmethod
    def _backfill_tenant_event_chain(conn, *, rebuild_all: bool = False) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, tenant_id, event_hash, tenant_event_hash
                FROM events ORDER BY id ASC
                """
            )
            heads: dict[str, str | None] = {}
            updated = 0
            for row in cur.fetchall():
                tenant_id = PostgresStore._as_text(row.get("tenant_id"))
                if tenant_id is None:
                    continue
                existing_hash = PostgresStore._as_text(row.get("tenant_event_hash"))
                if existing_hash and not rebuild_all:
                    heads[tenant_id] = existing_hash
                    continue
                event_hash = PostgresStore._as_text(row.get("event_hash"))
                if event_hash is None:
                    raise RuntimeError("Cannot build tenant event chain before global hashes exist.")
                previous = heads.get(tenant_id)
                tenant_hash = compute_tenant_event_hash(
                    tenant_id=tenant_id,
                    event_hash=event_hash,
                    tenant_prev_event_hash=previous,
                )
                cur.execute(
                    """
                    UPDATE events SET tenant_prev_event_hash = %s,
                        tenant_event_hash = %s, tenant_hash_algo = %s
                    WHERE id = %s
                    """,
                    (previous, tenant_hash, TENANT_EVENT_HASH_ALGO, int(row["id"])),
                )
                heads[tenant_id] = tenant_hash
                updated += 1
            return updated

    def _event_tenant_for_claim(self, cur, claim_id: int | None) -> str | None:
        if claim_id is None:
            return self._tenant_for_operation() if self.require_tenant else self.tenant_id
        cur.execute("SELECT tenant_id FROM claims WHERE id = %s", (claim_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Claim {claim_id} does not exist.")
        raw_tenant = row.get("tenant_id") if isinstance(row, dict) else row[0]
        claim_tenant = self._as_text(raw_tenant)
        if self.require_tenant and claim_tenant is None:
            raise PermissionError("Tenant-owned events require a tenant-owned claim.")
        return self._tenant_for_operation(claim_tenant)

    @staticmethod
    def _event_chain_head(
        cur,
        tenant_id: str | None,
    ) -> tuple[str | None, str, str | None]:
        lock_key = tenant_id or "__memorymaster_global_events__"
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"memorymaster:event:{lock_key}",),
        )
        if tenant_id is not None:
            cur.execute(
                """
                SELECT global_event_hash, tenant_event_hash
                FROM public.memorymaster_event_chain_head()
                """
            )
            row = cur.fetchone()
            primary = row.get("global_event_hash") if isinstance(row, dict) and row else None
            tenant = row.get("tenant_event_hash") if isinstance(row, dict) and row else None
            return (
                str(primary) if primary else None,
                POSTGRES_TENANT_EVENT_HASH_ALGO,
                str(tenant) if tenant else None,
            )
        if tenant_id is None:
            algo = EVENT_HASH_ALGO
            cur.execute(
                """
                SELECT event_hash FROM events
                WHERE event_hash IS NOT NULL
                  AND (hash_algo IS NULL OR hash_algo = %s)
                ORDER BY id DESC LIMIT 1
                """,
                (algo,),
            )
        row = cur.fetchone()
        value = row.get("event_hash") if isinstance(row, dict) and row else None
        return (str(value) if value else None), algo, None

    def _insert_event_row(
        self,
        conn,
        *,
        claim_id: int | None,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        details: str | None,
        payload: dict[str, object] | None,
        created_at: datetime,
    ) -> int:
        sanitized = sanitize_event_input(
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            details=details,
            payload=payload,
            created_at=created_at,
        )
        details = sanitized.details
        if sanitized.payload is not None and not isinstance(sanitized.payload, dict):
            raise ValueError("Event payload must be a JSON object.")
        payload = sanitized.payload
        _, _, Jsonb = self._load_psycopg()
        with conn.cursor() as cur:
            tenant_id = self._event_tenant_for_claim(cur, claim_id)
            validate_persisted_metadata({"effective_tenant_id": tenant_id})
            prev_event_hash, hash_algo, tenant_prev_event_hash = self._event_chain_head(
                cur,
                tenant_id,
            )
            event_hash = self._compute_event_hash(
                claim_id=claim_id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                details=details,
                payload=payload,
                created_at=created_at,
                prev_event_hash=prev_event_hash,
                tenant_id=tenant_id,
                hash_algo=hash_algo,
            )
            tenant_event_hash = (
                compute_tenant_event_hash(
                    tenant_id=tenant_id,
                    event_hash=event_hash,
                    tenant_prev_event_hash=tenant_prev_event_hash,
                )
                if tenant_id is not None
                else None
            )
            tenant_hash_algo = TENANT_EVENT_HASH_ALGO if tenant_id is not None else None
            cur.execute(
                """
                INSERT INTO events (
                    claim_id, event_type, from_status, to_status, details, payload_json, created_at,
                    prev_event_hash, event_hash, hash_algo, tenant_id,
                    tenant_prev_event_hash, tenant_event_hash, tenant_hash_algo
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    claim_id,
                    event_type,
                    from_status,
                    to_status,
                    details,
                    Jsonb(payload) if payload is not None else None,
                    created_at,
                    prev_event_hash,
                    event_hash,
                    hash_algo,
                    tenant_id,
                    tenant_prev_event_hash,
                    tenant_event_hash,
                    tenant_hash_algo,
                ),
            )
            inserted = cur.fetchone()
        if inserted is None:
            raise RuntimeError("Failed to insert event row.")
        return int(inserted["id"])

    def _assign_human_id(
        self,
        cur,
        *,
        subject: str | None,
        text: str,
        claim_id: int,
        tenant_id: str | None,
        scope: str,
        visibility: str,
        source_agent: str | None,
    ) -> str:
        psycopg, _, _ = self._load_psycopg()
        savepoint = "sp_claim_human_id_assignment"
        for _attempt in range(100):
            human_id = self._allocate_human_id(
                cur,
                subject,
                text,
                claim_id,
                tenant_id=tenant_id,
                scope=scope,
                visibility=visibility,
                source_agent=source_agent,
            )
            cur.execute(f"SAVEPOINT {savepoint}")
            try:
                cur.execute(
                    "UPDATE claims SET human_id = %s WHERE id = %s",
                    (human_id, claim_id),
                )
            except psycopg.errors.UniqueViolation as exc:
                cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
                if constraint not in POSTGRES_HUMAN_IDENTITY_INDEXES:
                    raise
                continue
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
            return human_id
        raise RuntimeError("Unable to allocate a unique human_id after 100 attempts.")

    def create_claim(
        self,
        text: str,
        citations: list[CitationInput],
        *,
        idempotency_key: str | None = None,
        claim_type: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        object_value: str | None = None,
        scope: str = "project",
        volatility: str = "medium",
        confidence: float = 0.5,
        tenant_id: str | None = None,
        event_time: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        source_agent: str | None = None,
        visibility: str = "public",
        holder: str | None = None,
    ) -> Claim:
        if not citations:
            raise ValueError("At least one citation is required.")
        citation_inputs = [
            CitationInput(
                source=cite.get("source", ""),
                locator=cite.get("locator"),
                excerpt=cite.get("excerpt"),
            )
            if isinstance(cite, dict)
            else CitationInput(cite.source, cite.locator, cite.excerpt)
            for cite in citations
        ]
        sanitized = sanitize_claim_input(
            text=text,
            object_value=object_value,
            citations=citation_inputs,
            subject=subject,
            predicate=predicate,
            idempotency_key=idempotency_key,
            claim_type=claim_type,
            scope=scope,
            volatility=volatility,
            source_agent=source_agent,
            visibility=visibility,
            holder=holder,
            confidence=confidence,
            event_time=event_time,
            valid_from=valid_from,
            valid_until=valid_until,
            tenant_id=tenant_id,
        )
        self._validate_bound_persistence_identity()
        text = sanitized.text
        object_value = sanitized.object_value
        citations = sanitized.citations
        subject = sanitized.subject
        predicate = sanitized.predicate
        visibility, source_agent = normalize_claim_identity(
            visibility,
            source_agent,
            allow_sensitive=not self.require_tenant,
        )
        normalized_idempotency_key = (idempotency_key or "").strip() or None
        normalized_tenant_id = self._tenant_for_operation(tenant_id)
        validate_persisted_metadata(
            {
                "effective_source_agent": source_agent,
                "effective_tenant_id": normalized_tenant_id,
            }
        )
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO claims (
                        text, idempotency_key, normalized_text, claim_type, subject, predicate, object_value,
                        scope, volatility, status, confidence, pinned, supersedes_claim_id,
                        replaced_by_claim_id, created_at, updated_at, last_validated_at, archived_at,
                        tenant_id, event_time, valid_from, valid_until, source_agent, visibility, holder
                    ) VALUES (
                        %s, %s, NULL, %s, %s, %s, %s, %s, %s, 'candidate', %s, FALSE, NULL, NULL, %s, %s, NULL, NULL,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                (
                    text,
                    normalized_idempotency_key,
                    claim_type,
                    subject,
                    predicate,
                    object_value,
                    scope,
                    volatility,
                    confidence,
                    now,
                    now,
                    normalized_tenant_id,
                    event_time,
                    valid_from if valid_from is not None else now,
                    valid_until,
                    source_agent,
                    visibility,
                    holder,
                ),
            )
            claim_row = cur.fetchone()
            if claim_row is None:
                if normalized_idempotency_key is None:
                    raise RuntimeError("Failed to create claim.")
                identity_sql, identity_params = self._postgres_identity_filter(
                    visibility,
                    source_agent,
                )
                cur.execute(
                    f"""
                    SELECT id FROM claims
                    WHERE idempotency_key = %s
                      AND tenant_id IS NOT DISTINCT FROM %s
                      AND scope = %s
                      AND {identity_sql}
                    """,
                    (
                        normalized_idempotency_key,
                        normalized_tenant_id,
                        scope,
                        *identity_params,
                    ),
                )
                existing_row = cur.fetchone()
                if existing_row is None:
                    raise RuntimeError("Idempotency key matched missing claim.")
                claim_id = int(existing_row["id"])
                claim = self.get_claim(claim_id)
                if claim is None:
                    raise RuntimeError("Idempotency key matched missing claim.")
                return claim
            claim_id = int(claim_row["id"])

            self._assign_human_id(
                cur,
                subject=subject,
                text=text,
                claim_id=claim_id,
                tenant_id=normalized_tenant_id,
                scope=scope,
                visibility=visibility,
                source_agent=source_agent,
            )

            for cite in citations:
                cur.execute(
                    """
                        INSERT INTO citations (claim_id, source, locator, excerpt, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                    (claim_id, cite.source, cite.locator, cite.excerpt, now),
                )
            ingest_payload = validate_event_payload(
                "ingest",
                {"citation_count": len(citations)},
                details="claim_ingested",
            )
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="ingest",
                from_status=None,
                to_status="candidate",
                details="claim_ingested",
                payload=ingest_payload,
                created_at=now,
            )
            if sanitized.is_sensitive:
                policy_payload = validate_event_payload(
                    "policy_decision",
                    {"findings": sanitized.findings},
                    details="sensitive_redaction_applied",
                )
                self._insert_event_row(
                    conn,
                    claim_id=claim_id,
                    event_type="policy_decision",
                    from_status="candidate",
                    to_status="candidate",
                    details="sensitive_redaction_applied",
                    payload=policy_payload,
                    created_at=now,
                )

        claim = self.get_claim(claim_id)
        if claim is None:
            raise RuntimeError("Failed to load claim after insert.")
        return claim

    def get_claim(self, claim_id: int, include_citations: bool = True) -> Claim | None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM claims WHERE id = %s", (claim_id,))
            row = cur.fetchone()
        if row is None:
            return None
        claim = self._row_to_claim(row)
        if include_citations:
            claim.citations = self.list_citations(claim.id)
        return claim

    def _select_claim_identity_rows(
        self,
        column: str,
        value: str,
        *,
        tenant_id: str | None,
        scope: str | None,
        visibility: str,
        source_agent: str | None,
    ) -> list[Any]:
        if column not in {"idempotency_key", "human_id"}:
            raise ValueError(f"Unsupported claim identity column: {column}")
        visibility, source_agent = normalize_claim_identity(
            visibility,
            source_agent,
            allow_sensitive=not self.require_tenant,
        )
        identity_sql, identity_params = self._postgres_identity_filter(
            visibility,
            source_agent,
        )
        clauses = [
            f"{column} = %s",
            identity_sql,
            "tenant_id IS NOT DISTINCT FROM %s",
        ]
        params: list[object] = [
            value,
            *identity_params,
            self._tenant_for_operation(tenant_id),
        ]
        if scope is not None:
            clauses.append("scope = %s")
            params.append(scope)
        sql = f"SELECT * FROM claims WHERE {' AND '.join(clauses)} LIMIT 2"
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def _claim_from_identity_rows(
        self,
        rows: list[Any],
        *,
        identifier: str,
        include_citations: bool,
    ) -> Claim | None:
        row = require_unambiguous_identity_row(rows, identifier=identifier)
        if row is None:
            return None
        claim = self._row_to_claim(row)
        if include_citations:
            claim.citations = self.list_citations(claim.id)
        return claim

    def get_claim_by_idempotency_key(
        self,
        idempotency_key: str,
        include_citations: bool = True,
        *,
        tenant_id: str | None = None,
        scope: str | None = None,
        visibility: str = "public",
        source_agent: str | None = None,
    ) -> Claim | None:
        normalized_idempotency_key = idempotency_key.strip()
        if not normalized_idempotency_key:
            return None
        rows = self._select_claim_identity_rows(
            "idempotency_key",
            normalized_idempotency_key,
            tenant_id=tenant_id,
            scope=scope,
            visibility=visibility,
            source_agent=source_agent,
        )
        return self._claim_from_identity_rows(
            rows,
            identifier="idempotency key",
            include_citations=include_citations,
        )

    def claim_ids_by_source_agent(
        self,
        source_agent: str,
        *,
        include_archived: bool = False,
    ) -> list[int]:
        """Postgres parity for :meth:`SQLiteStore.claim_ids_by_source_agent`."""
        clauses = ["source_agent = %s"]
        params: list[object] = [source_agent]
        if not include_archived:
            clauses.append("status <> 'archived'")
        sql = f"SELECT id FROM claims WHERE {' AND '.join(clauses)} ORDER BY id DESC"
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [int(row["id"]) for row in rows]

    def list_claims(
        self,
        *,
        status: str | None = None,
        status_in: list[str] | None = None,
        limit: int = 50,
        include_archived: bool = False,
        text_query: str | None = None,
        include_citations: bool = False,
        scope_allowlist: list[str] | None = None,
        tenant_id: str | None = None,
        holder: str | None = None,
    ) -> list[Claim]:
        clauses: list[str] = []
        params: list[object] = []

        if tenant_id is not None:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)

        # takes-vs-facts holder filter — parity with SQLiteStore._build_list_clauses.
        if holder is not None and holder.strip():
            clauses.append("holder = %s")
            params.append(holder.strip())

        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        elif status_in:
            placeholders = ",".join("%s" for _ in status_in)
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_in)

        if not include_archived and status != "archived":
            clauses.append("status <> 'archived'")

        if text_query:
            clauses.append("(LOWER(text) LIKE %s OR LOWER(COALESCE(normalized_text, '')) LIKE %s)")
            needle = f"%{text_query.lower()}%"
            params.extend([needle, needle])

        if scope_allowlist:
            normalized_scopes = [scope.strip() for scope in scope_allowlist if scope and scope.strip()]
            if normalized_scopes:
                placeholders = ",".join("%s" for _ in normalized_scopes)
                clauses.append(f"scope IN ({placeholders})")
                params.extend(normalized_scopes)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT * FROM claims
            {where_sql}
            ORDER BY pinned DESC, confidence DESC, updated_at DESC, id DESC
            LIMIT %s
        """
        params.append(limit)

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        claims = [self._row_to_claim(row) for row in rows]
        if include_citations:
            for claim in claims:
                claim.citations = self.list_citations(claim.id)
        return claims

    def list_citations(self, claim_id: int) -> list[Citation]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM citations WHERE claim_id = %s ORDER BY id ASC",
                (claim_id,),
            )
            rows = cur.fetchall()
        return [self._row_to_citation(row) for row in rows]

    def list_events(
        self,
        claim_id: int | None = None,
        limit: int = 100,
        event_type: str | None = None,
    ) -> list[Event]:
        clauses: list[str] = []
        params: list[object] = []

        if self.tenant_id is not None:
            clauses.append("tenant_id IS NOT DISTINCT FROM %s")
            params.append(self._tenant_for_operation())

        if claim_id is not None:
            clauses.append("claim_id = %s")
            params.append(claim_id)
        if event_type is not None:
            clauses.append("event_type = %s")
            params.append(event_type)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM events {where_sql} ORDER BY created_at DESC, id DESC LIMIT %s"
        params.append(limit)

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_event(row) for row in rows]

    def count_citations(self, claim_id: int) -> int:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM citations WHERE claim_id = %s", (claim_id,))
            row = cur.fetchone()
        return int(row["c"]) if row is not None else 0

    def list_citations_batch(self, claim_ids: list[int]) -> dict[int, list[Citation]]:
        if not claim_ids:
            return {}
        return {claim_id: self.list_citations(claim_id) for claim_id in claim_ids}

    def count_citations_batch(self, claim_ids: list[int]) -> dict[int, int]:
        if not claim_ids:
            return {}
        return {claim_id: self.count_citations(claim_id) for claim_id in claim_ids}

    def set_normalized_text(self, claim_id: int, normalized_text: str) -> None:
        sanitized_text, _ = sanitize_persisted_text(normalized_text)
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE claims SET normalized_text = %s, updated_at = %s WHERE id = %s",
                (sanitized_text, now, claim_id),
            )

    def set_normalized_texts_batch(self, updates: dict[int, str]) -> None:
        if not updates:
            return
        sanitized_updates = {
            claim_id: sanitize_persisted_text(normalized_text)[0]
            for claim_id, normalized_text in updates.items()
        }
        for claim_id, normalized_text in sanitized_updates.items():
            self.set_normalized_text(claim_id, normalized_text)

    def redact_claim_payload(
        self,
        claim_id: int,
        *,
        mode: str = "redact",
        redact_claim: bool = True,
        redact_citations: bool = True,
        reason: str | None = None,
        actor: str = "system",
    ) -> dict[str, object]:
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"redact", "erase"}:
            raise ValueError("mode must be one of: redact, erase.")
        if not redact_claim and not redact_citations:
            raise ValueError("At least one of redact_claim or redact_citations must be true.")

        now = utc_now()
        details = "claim_payload_redacted" if normalized_mode == "redact" else "claim_payload_erased"
        claim_text = "[REDACTED_CLAIM_TEXT]" if normalized_mode == "redact" else "[ERASED_CLAIM_TEXT]"
        subject_value = "[REDACTED]" if normalized_mode == "redact" else None
        predicate_value = "[REDACTED]" if normalized_mode == "redact" else None
        object_value = "[REDACTED]" if normalized_mode == "redact" else None
        citation_source = "[REDACTED_SOURCE]" if normalized_mode == "redact" else "[ERASED_SOURCE]"
        citation_locator = "[REDACTED_LOCATOR]" if normalized_mode == "redact" else None
        citation_excerpt = "[REDACTED_EXCERPT]" if normalized_mode == "redact" else None

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM claims WHERE id = %s", (claim_id,))
            status_row = cur.fetchone()
            if status_row is None:
                raise ValueError(f"Claim {claim_id} does not exist.")
            current_status = str(status_row["status"]) if status_row["status"] is not None else None

            claim_rows = 0
            citation_rows = 0

            if redact_claim:
                cur.execute(
                    """
                        UPDATE claims
                        SET text = %s,
                            normalized_text = NULL,
                            subject = %s,
                            predicate = %s,
                            object_value = %s,
                            updated_at = %s
                        WHERE id = %s
                        """,
                    (claim_text, subject_value, predicate_value, object_value, now, claim_id),
                )
                claim_rows = int(cur.rowcount)

            if redact_citations:
                cur.execute(
                    """
                        UPDATE citations
                        SET source = %s, locator = %s, excerpt = %s
                        WHERE claim_id = %s
                        """,
                    (citation_source, citation_locator, citation_excerpt, claim_id),
                )
                citation_rows = int(cur.rowcount)
                if not redact_claim:
                    cur.execute(
                        "UPDATE claims SET updated_at = %s WHERE id = %s",
                        (now, claim_id),
                    )

            payload: dict[str, object] = {
                "source": str(actor or "system"),
                "mode": normalized_mode,
                "redact_claim": bool(redact_claim),
                "redact_citations": bool(redact_citations),
                "claim_rows": claim_rows,
                "citation_rows": citation_rows,
            }
            if reason and reason.strip():
                payload["reason"] = reason.strip()
            validated_payload = validate_event_payload("audit", payload, details=details)
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="audit",
                from_status=current_status,
                to_status=current_status,
                details=details,
                payload=validated_payload,
                created_at=now,
            )

        return {
            "claim_id": claim_id,
            "mode": normalized_mode,
            "redact_claim": bool(redact_claim),
            "redact_citations": bool(redact_citations),
            "claim_rows": claim_rows,
            "citation_rows": citation_rows,
            "event_details": details,
        }

    def update_claim_structure(
        self,
        claim_id: int,
        *,
        claim_type: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        object_value: str | None = None,
    ) -> None:
        sanitized = sanitize_claim_structure_input(
            claim_type=claim_type,
            subject=subject,
            predicate=predicate,
            object_value=object_value,
        )
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    UPDATE claims
                    SET claim_type = COALESCE(claim_type, %s),
                        subject = COALESCE(subject, %s),
                        predicate = COALESCE(predicate, %s),
                        object_value = COALESCE(object_value, %s),
                        updated_at = %s
                    WHERE id = %s
                    """,
                (
                    sanitized.claim_type,
                    sanitized.subject,
                    sanitized.predicate,
                    sanitized.object_value,
                    now,
                    claim_id,
                ),
            )

    def set_confidence(self, claim_id: int, confidence: float, details: str | None = None) -> None:
        bounded = max(0.0, min(1.0, confidence))
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE claims SET confidence = %s, updated_at = %s WHERE id = %s",
                (bounded, now, claim_id),
            )
            if details:
                cur.execute("SELECT status FROM claims WHERE id = %s", (claim_id,))
                status_row = cur.fetchone()
                current_status = str(status_row["status"]) if status_row else None
                self._insert_event_row(
                    conn,
                    claim_id=claim_id,
                    event_type="confidence",
                    from_status=current_status,
                    to_status=current_status,
                    details=details,
                    payload=None,
                    created_at=now,
                )

    def set_pinned(self, claim_id: int, pinned: bool, reason: str) -> None:
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE claims SET pinned = %s, updated_at = %s WHERE id = %s",
                (pinned, now, claim_id),
            )
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="pin" if pinned else "unpin",
                from_status=None,
                to_status=None,
                details=reason,
                payload=None,
                created_at=now,
            )

    def apply_status_transition(
        self,
        claim: Claim,
        *,
        to_status: str,
        reason: str,
        event_type: str,
        replaced_by_claim_id: int | None = None,
    ) -> Claim:
        validated_event_type = validate_transition_event_type(event_type)
        now = utc_now()
        last_validated_at = now if to_status in {"confirmed", "stale", "conflicted"} else claim.last_validated_at
        archived_at = now if to_status == "archived" else None
        next_replaced_by = replaced_by_claim_id if replaced_by_claim_id is not None else claim.replaced_by_claim_id

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE claims
                SET status = %s, updated_at = %s, last_validated_at = %s, archived_at = %s, replaced_by_claim_id = %s
                WHERE id = %s
                """,
                (to_status, now, last_validated_at, archived_at, next_replaced_by, claim.id),
            )
            self._insert_event_row(
                conn,
                claim_id=claim.id,
                event_type=validated_event_type,
                from_status=claim.status,
                to_status=to_status,
                details=reason,
                payload={"replaced_by_claim_id": replaced_by_claim_id} if replaced_by_claim_id else None,
                created_at=now,
            )

        updated = self.get_claim(claim.id)
        if updated is None:
            raise RuntimeError("Failed to load claim after transition.")
        return updated

    def recompute_tiers(self) -> dict[str, int]:
        """Postgres override of the SQLite tier recompute — same rules.

        The inherited ``_LifecycleMixin.recompute_tiers`` uses sqlite-style
        ``?`` placeholders, which psycopg rejects (Postgres sees a literal
        ``?`` and raises a syntax error), so on Postgres the steward's
        recompute-tiers phase always failed and tiers were never updated.
        Same thresholds and bucket semantics as the SQLite version; ``%s``
        placeholders and native datetimes for the TIMESTAMPTZ ``created_at``.
        """
        now = datetime.now(timezone.utc)
        core_cutoff = (now - timedelta(days=7)).replace(microsecond=0)
        peripheral_cutoff = (now - timedelta(days=90)).replace(microsecond=0)

        counts = {"core": 0, "working": 0, "peripheral": 0}
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE claims SET tier = 'core' "
                "WHERE status != 'archived' AND tier != 'core' "
                "AND (access_count > 5 OR created_at > %s)",
                (core_cutoff,),
            )
            counts["core"] = cur.rowcount
            cur.execute(
                "UPDATE claims SET tier = 'peripheral' "
                "WHERE status != 'archived' AND tier != 'peripheral' "
                "AND access_count = 0 AND created_at <= %s",
                (peripheral_cutoff,),
            )
            counts["peripheral"] = cur.rowcount
            cur.execute(
                "UPDATE claims SET tier = 'working' "
                "WHERE status != 'archived' AND tier != 'working' "
                "AND NOT (access_count > 5 OR created_at > %s) "
                "AND NOT (access_count = 0 AND created_at <= %s)",
                (core_cutoff, peripheral_cutoff),
            )
            counts["working"] = cur.rowcount
            conn.commit()
        return counts

    def set_supersedes(self, claim_id: int, supersedes_claim_id: int) -> None:
        self.mark_superseded(
            supersedes_claim_id,
            claim_id,
            "set_supersedes compatibility path",
        )

    def mark_superseded(self, old_claim_id: int, new_claim_id: int, reason: str) -> None:
        if old_claim_id == new_claim_id:
            raise ValueError("Supersession claims are unavailable.")
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, version, replaced_by_claim_id,
                       supersedes_claim_id
                FROM claims
                WHERE id IN (%s, %s)
                ORDER BY id
                FOR UPDATE
                """,
                (old_claim_id, new_claim_id),
            )
            rows = {int(row["id"]): row for row in cur.fetchall()}
            if set(rows) != {old_claim_id, new_claim_id}:
                raise ValueError("Supersession claims are unavailable.")
            self._apply_atomic_supersession(
                conn,
                cur,
                rows[old_claim_id],
                rows[new_claim_id],
                reason,
                now,
            )

    def _apply_atomic_supersession(
        self,
        conn,
        cur,
        old: dict[str, object],
        new: dict[str, object],
        reason: str,
        now: datetime,
    ) -> None:
        old_id = int(old["id"])
        new_id = int(new["id"])
        if old.get("status") == "superseded" or old.get("replaced_by_claim_id") is not None:
            raise ConcurrentModificationError(
                f"Claim {old_id} was already superseded. Reload and retry."
            )
        old_status = str(old.get("status") or "")
        if not can_transition(old_status, "superseded"):
            raise ValueError(f"Invalid transition: {old_status} -> superseded")
        if new.get("supersedes_claim_id") not in {None, old_id}:
            raise ConcurrentModificationError(
                f"Claim {new_id} already supersedes another claim. Reload and retry."
            )
        self._update_superseded_claim(cur, old, new_id, now)
        self._update_replacement_claim(cur, new, old_id, now)
        self._insert_event_row(
            conn,
            claim_id=old_id,
            event_type="supersession",
            from_status=str(old.get("status") or "candidate"),
            to_status="superseded",
            details=reason,
            payload={"replaced_by_claim_id": new_id},
            created_at=now,
        )

    @staticmethod
    def _update_superseded_claim(cur, old: dict[str, object], new_id: int, now) -> None:
        old_id = int(old["id"])
        cur.execute(
            """
            UPDATE claims
            SET status = 'superseded', updated_at = %s, replaced_by_claim_id = %s,
                version = version + 1, valid_until = COALESCE(%s, valid_until)
            WHERE id = %s AND version = %s AND status != 'superseded'
              AND replaced_by_claim_id IS NULL
            """,
            (now, new_id, now, old_id, int(old.get("version") or 1)),
        )
        if cur.rowcount != 1:
            raise ConcurrentModificationError(
                f"Claim {old_id} was modified by another writer. Reload and retry."
            )
    @staticmethod
    def _update_replacement_claim(cur, new: dict[str, object], old_id: int, now) -> None:
        new_id = int(new["id"])
        cur.execute(
            """
            UPDATE claims
            SET supersedes_claim_id = %s, updated_at = %s
            WHERE id = %s AND (supersedes_claim_id IS NULL OR supersedes_claim_id = %s)
            """,
            (old_id, now, new_id, old_id),
        )
        if cur.rowcount != 1:
            raise ConcurrentModificationError(
                f"Claim {new_id} was modified by another writer. Reload and retry."
            )

    def find_by_status(self, status: str, limit: int = 100, include_citations: bool = False) -> list[Claim]:
        return self.list_claims(
            status=status,
            limit=limit,
            include_archived=True,
            include_citations=include_citations,
        )

    def find_for_decay(self, limit: int = 200) -> list[Claim]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT * FROM claims
                    WHERE status = 'confirmed'
                      AND pinned = FALSE
                    ORDER BY updated_at ASC, id ASC
                    LIMIT %s
                    """,
                (limit,),
            )
            rows = cur.fetchall()
        return [self._row_to_claim(row) for row in rows]

    def find_for_compaction(self, retain_days: int, limit: int = 500) -> list[Claim]:
        cutoff = utc_now() - timedelta(days=retain_days)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT * FROM claims
                    WHERE status IN ('stale', 'superseded', 'conflicted')
                      AND pinned = FALSE
                      AND updated_at < %s
                    ORDER BY updated_at ASC, id ASC
                    LIMIT %s
                    """,
                (cutoff, limit),
            )
            rows = cur.fetchall()
        return [self._row_to_claim(row) for row in rows]

    def find_confirmed_by_tuple(
        self,
        *,
        subject: str | None,
        predicate: str | None,
        scope: str | None,
        exclude_claim_id: int | None = None,
        tenant_id: str | None = None,
        visibility: str = "public",
        source_agent: str | None = None,
    ) -> list[Claim]:
        if not subject or not predicate:
            return []

        clauses = ["status = 'confirmed'", "subject = %s", "predicate = %s", "scope = %s"]
        params: list[object] = [subject, predicate, scope or "project"]
        visibility, source_agent = normalize_claim_identity(
            visibility,
            source_agent,
            allow_sensitive=not self.require_tenant,
        )
        identity_sql, identity_params = self._postgres_identity_filter(
            visibility,
            source_agent,
        )
        clauses.append(identity_sql)
        params.extend(identity_params)
        clauses.append("tenant_id IS NOT DISTINCT FROM %s")
        params.append(self._tenant_for_operation(tenant_id))
        if exclude_claim_id is not None:
            clauses.append("id <> %s")
            params.append(exclude_claim_id)

        sql = f"""
            SELECT * FROM claims
            WHERE {' AND '.join(clauses)}
            ORDER BY confidence DESC, updated_at DESC
        """
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_claim(row) for row in rows]

    def delete_old_events(self, retain_days: int) -> int:
        # Events are append-only by contract; retention trim is a no-op.
        return 0

    @staticmethod
    def _event_chain_link_issues(
        rows: list[dict[str, object]],
        limit: int,
    ) -> list[dict[str, object]]:
        issues: list[dict[str, object]] = []
        v1_expected: str | None = None
        tenant_expected: dict[str, str | None] = {}
        for row in rows:
            event_id = int(row["id"])
            row_prev = PostgresStore._as_text(row.get("prev_event_hash"))
            row_hash = PostgresStore._as_text(row.get("event_hash"))
            row_algo = PostgresStore._as_text(row.get("hash_algo")) or EVENT_HASH_ALGO
            tenant_id = PostgresStore._as_text(row.get("tenant_id"))
            if row_hash is None:
                issues.append({"event_id": event_id, "reason": "missing_hash"})
                continue
            if row_algo == EVENT_HASH_ALGO:
                expected = v1_expected
                v1_expected = row_hash
            elif row_algo == POSTGRES_TENANT_EVENT_HASH_ALGO and tenant_id is not None:
                expected = tenant_expected.get(tenant_id)
                tenant_expected[tenant_id] = row_hash
            else:
                issues.append(
                    {
                        "event_id": event_id,
                        "reason": "unexpected_hash_algo_or_tenant",
                        "hash_algo": row_algo,
                        "tenant_id": tenant_id,
                    }
                )
                continue
            if row_prev != expected:
                issues.append(
                    {
                        "event_id": event_id,
                        "reason": "broken_prev_link",
                        "expected_prev_event_hash": expected,
                        "actual_prev_event_hash": row_prev,
                    }
                )
            if len(issues) >= limit:
                break
        return issues

    @staticmethod
    def _expected_primary_event_hash(
        row: dict[str, object],
        *,
        canonicalize_timestamp: bool = True,
    ) -> str | None:
        tenant_id = PostgresStore._as_text(row.get("tenant_id"))
        event_type = PostgresStore._as_text(row.get("event_type"))
        created_at = row.get("created_at")
        hash_algo = PostgresStore._as_text(row.get("hash_algo")) or EVENT_HASH_ALGO
        if hash_algo not in {EVENT_HASH_ALGO, POSTGRES_TENANT_EVENT_HASH_ALGO}:
            return None
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                return None
        if event_type is None or not isinstance(created_at, datetime):
            return None
        if hash_algo == POSTGRES_TENANT_EVENT_HASH_ALGO and tenant_id is None:
            return None
        claim_id = row.get("claim_id")
        return PostgresStore._compute_event_hash(
            claim_id=int(claim_id) if claim_id is not None else None,
            event_type=event_type,
            from_status=PostgresStore._as_text(row.get("from_status")),
            to_status=PostgresStore._as_text(row.get("to_status")),
            details=PostgresStore._as_text(row.get("details")),
            payload=row.get("payload_json"),
            created_at=created_at,
            prev_event_hash=PostgresStore._as_text(row.get("prev_event_hash")),
            tenant_id=tenant_id,
            hash_algo=hash_algo,
            canonicalize_timestamp=canonicalize_timestamp,
        )

    @staticmethod
    def _expected_v2_event_hash(row: dict[str, object]) -> str | None:
        if PostgresStore._as_text(row.get("hash_algo")) != POSTGRES_TENANT_EVENT_HASH_ALGO:
            return None
        return PostgresStore._expected_primary_event_hash(row)

    @staticmethod
    def _event_content_issues(
        rows: list[dict[str, object]],
        limit: int,
    ) -> list[dict[str, object]]:
        issues: list[dict[str, object]] = []
        for row in rows:
            hash_algo = PostgresStore._as_text(row.get("hash_algo")) or EVENT_HASH_ALGO
            if hash_algo not in {EVENT_HASH_ALGO, POSTGRES_TENANT_EVENT_HASH_ALGO}:
                continue
            expected_hash = PostgresStore._expected_primary_event_hash(row)
            stored_hash = PostgresStore._as_text(row.get("event_hash"))
            if expected_hash is None:
                issues.append(
                    {"event_id": int(row["id"]), "reason": "missing_event_hash_material"}
                )
            else:
                expected_hashes = {expected_hash}
                hash_algo = PostgresStore._as_text(row.get("hash_algo")) or EVENT_HASH_ALGO
                if hash_algo == EVENT_HASH_ALGO:
                    legacy_hash = PostgresStore._expected_primary_event_hash(
                        row,
                        canonicalize_timestamp=False,
                    )
                    if legacy_hash is not None:
                        expected_hashes.add(legacy_hash)
                if stored_hash not in expected_hashes:
                    issues.append(
                        {"event_id": int(row["id"]), "reason": "event_hash_mismatch"}
                    )
            if len(issues) >= limit:
                break
        return issues

    @staticmethod
    def _event_chain_issues(
        rows: list[dict[str, object]],
        limit: int,
        *,
        verify_content: bool = True,
    ) -> list[dict[str, object]]:
        issues = PostgresStore._event_chain_link_issues(rows, limit)
        if verify_content and len(issues) < limit:
            issues.extend(PostgresStore._event_content_issues(rows, limit - len(issues)))
        return issues

    @staticmethod
    def _tenant_event_chain_issues(
        rows: list[dict[str, object]],
        limit: int,
    ) -> list[dict[str, object]]:
        issues: list[dict[str, object]] = []
        heads: dict[str, str | None] = {}
        for row in rows:
            event_id = int(row["id"])
            tenant_id = PostgresStore._as_text(row.get("tenant_id"))
            if tenant_id is None:
                continue
            previous = PostgresStore._as_text(row.get("tenant_prev_event_hash"))
            stored_hash = PostgresStore._as_text(row.get("tenant_event_hash"))
            hash_algo = PostgresStore._as_text(row.get("tenant_hash_algo"))
            event_hash = PostgresStore._as_text(row.get("event_hash"))
            expected_previous = heads.get(tenant_id)
            if previous != expected_previous:
                issues.append(
                    {
                        "event_id": event_id,
                        "reason": "broken_tenant_prev_link",
                        "expected_prev_event_hash": expected_previous,
                        "actual_prev_event_hash": previous,
                    }
                )
            if hash_algo != TENANT_EVENT_HASH_ALGO or event_hash is None or stored_hash is None:
                issues.append({"event_id": event_id, "reason": "missing_tenant_hash_material"})
            else:
                expected_hash = compute_tenant_event_hash(
                    tenant_id=tenant_id,
                    event_hash=event_hash,
                    tenant_prev_event_hash=previous,
                )
                if stored_hash != expected_hash:
                    issues.append({"event_id": event_id, "reason": "tenant_hash_mismatch"})
            heads[tenant_id] = stored_hash
            if len(issues) >= limit:
                break
        return issues

    def _reconcile_tenant_event_integrity(self, limit: int) -> dict[str, object]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, claim_id, event_type, from_status, to_status, details,
                       payload_json, created_at, prev_event_hash, event_hash, hash_algo,
                       tenant_id, tenant_prev_event_hash, tenant_event_hash, tenant_hash_algo
                FROM events
                WHERE tenant_id IS NOT DISTINCT FROM %s
                ORDER BY id ASC
                """,
                (self._tenant_for_operation(),),
            )
            rows = cur.fetchall()
            v2_rows = [
                row
                for row in rows
                if self._as_text(row.get("hash_algo")) == POSTGRES_TENANT_EVENT_HASH_ALGO
            ]
            original_chain_issues = self._event_chain_link_issues(v2_rows, limit)
            content_issues = self._event_content_issues(rows, limit)
            chain_issues = self._tenant_event_chain_issues(rows, limit)
        return {
            "checked_at": utc_now().isoformat(),
            "fix_mode": False,
            "issues": {
                "hash_chain_issues": original_chain_issues,
                "event_content_issues": content_issues,
                "tenant_hash_chain_issues": chain_issues,
            },
            "summary": {
                "hash_chain_issues": len(original_chain_issues),
                "event_content_issues": len(content_issues),
                "tenant_hash_chain_issues": len(chain_issues),
            },
            "actions": [],
        }

    def reconcile_integrity(self, *, fix: bool = False, limit: int = 500) -> dict[str, object]:
        if self.require_tenant:
            if fix:
                raise PermissionError(
                    "Integrity repair requires a privileged maintenance store."
                )
            return self._reconcile_tenant_event_integrity(limit)
        report: dict[str, object] = {
            "checked_at": utc_now().isoformat(),
            "fix_mode": bool(fix),
            "issues": {},
            "actions": [],
        }
        with self.connect() as conn:
            if fix:
                self._ensure_event_integrity_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT e.id
                    FROM events e
                    LEFT JOIN claims c ON c.id = e.claim_id
                    WHERE e.claim_id IS NOT NULL AND c.id IS NULL
                    ORDER BY e.id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                orphan_events = [int(row["id"]) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT ci.id
                    FROM citations ci
                    LEFT JOIN claims c ON c.id = ci.claim_id
                    WHERE c.id IS NULL
                    ORDER BY ci.id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                orphan_citations = [int(row["id"]) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT id
                    FROM claims
                    WHERE status = 'superseded' AND replaced_by_claim_id IS NULL
                    ORDER BY id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                superseded_without_replacement = [int(row["id"]) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT c.id
                    FROM claims c
                    LEFT JOIN claims n ON n.id = c.replaced_by_claim_id
                    WHERE c.replaced_by_claim_id IS NOT NULL AND n.id IS NULL
                    ORDER BY c.id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                dangling_replaced_by = [int(row["id"]) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT c.id
                    FROM claims c
                    LEFT JOIN claims p ON p.id = c.supersedes_claim_id
                    WHERE c.supersedes_claim_id IS NOT NULL AND p.id IS NULL
                    ORDER BY c.id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                dangling_supersedes = [int(row["id"]) for row in cur.fetchall()]

                transition_placeholders = ",".join("%s" for _ in STATUS_TRANSITION_EVENT_TYPES)
                cur.execute(
                    f"""
                    SELECT id, event_type, from_status, to_status
                    FROM events
                    WHERE event_type IN ({transition_placeholders})
                    ORDER BY id ASC
                    """,
                    list(STATUS_TRANSITION_EVENT_TYPES),
                )
                transition_rows = cur.fetchall()
                transition_issues: list[dict[str, object]] = []
                from memorymaster.core.lifecycle import ALLOWED_TRANSITIONS

                for row in transition_rows:
                    from_status = self._as_text(row["from_status"])
                    to_status = self._as_text(row["to_status"])
                    if from_status is None or to_status is None:
                        continue
                    if from_status not in CLAIM_STATUSES or to_status not in CLAIM_STATUSES:
                        transition_issues.append(
                            {
                                "event_id": int(row["id"]),
                                "event_type": str(row["event_type"]),
                                "reason": "unknown_status",
                                "from_status": from_status,
                                "to_status": to_status,
                            }
                        )
                        continue
                    if from_status == to_status:
                        continue
                    if to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
                        transition_issues.append(
                            {
                                "event_id": int(row["id"]),
                                "event_type": str(row["event_type"]),
                                "reason": "invalid_transition",
                                "from_status": from_status,
                                "to_status": to_status,
                            }
                        )

                cur.execute(
                    """
                    SELECT id, claim_id, event_type, from_status, to_status, details,
                           payload_json, created_at, prev_event_hash, event_hash, hash_algo,
                           tenant_id, tenant_prev_event_hash, tenant_event_hash, tenant_hash_algo
                    FROM events ORDER BY id ASC
                    """
                )
                chain_rows = cur.fetchall()
                chain_issues = self._event_chain_issues(chain_rows, limit)
                tenant_chain_issues = self._tenant_event_chain_issues(chain_rows, limit)

                issues = {
                    "orphan_events": orphan_events,
                    "orphan_citations": orphan_citations,
                    "superseded_without_replacement": superseded_without_replacement,
                    "dangling_replaced_by": dangling_replaced_by,
                    "dangling_supersedes": dangling_supersedes,
                    "transition_issues": transition_issues[:limit],
                    "hash_chain_issues": chain_issues[:limit],
                    "tenant_hash_chain_issues": tenant_chain_issues[:limit],
                }
                report["issues"] = issues
                report["summary"] = {
                    key: (len(value) if isinstance(value, list) else 0)
                    for key, value in issues.items()
                }

                actions: list[dict[str, object]] = []
                if fix:
                    if orphan_citations:
                        cur.execute("DELETE FROM citations WHERE id = ANY(%s)", (orphan_citations,))
                        actions.append({"action": "delete_orphan_citations", "rows": int(cur.rowcount)})
                    if orphan_events:
                        actions.append(
                            {
                                "action": "skip_delete_orphan_events_append_only",
                                "rows": 0,
                                "reason": "events table is append-only",
                            }
                        )
                    if dangling_replaced_by:
                        cur.execute(
                            "UPDATE claims SET replaced_by_claim_id = NULL WHERE id = ANY(%s)",
                            (dangling_replaced_by,),
                        )
                        actions.append({"action": "clear_dangling_replaced_by", "rows": int(cur.rowcount)})
                    if dangling_supersedes:
                        cur.execute(
                            "UPDATE claims SET supersedes_claim_id = NULL WHERE id = ANY(%s)",
                            (dangling_supersedes,),
                        )
                        actions.append({"action": "clear_dangling_supersedes", "rows": int(cur.rowcount)})
                    if chain_issues:
                        actions.append(
                            {
                                "action": "skip_rebuild_event_hash_chain_append_only",
                                "rows": 0,
                                "reason": "events table is append-only",
                            }
                        )
                report["actions"] = actions
        return report

    def record_event(
        self,
        *,
        claim_id: int | None,
        event_type: str,
        from_status: str | None = None,
        to_status: str | None = None,
        details: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self._validate_bound_persistence_identity()
        now = utc_now()
        sanitized = sanitize_event_input(
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            details=details,
            payload=payload,
            created_at=now,
        )
        details = sanitized.details
        if sanitized.payload is not None and not isinstance(sanitized.payload, dict):
            raise ValueError("Event payload must be a JSON object.")
        validated_event_type = validate_event_type(event_type)
        validated_payload = validate_event_payload(
            validated_event_type,
            sanitized.payload,
            details=details,
        )
        with self.connect() as conn:
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type=validated_event_type,
                from_status=from_status,
                to_status=to_status,
                details=details,
                payload=validated_payload,
                created_at=now,
            )

    def _has_vector_table(self) -> bool:
        if self._vector_table_available is not None:
            return self._vector_table_available
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT EXISTS (
                      SELECT 1
                      FROM information_schema.tables
                      WHERE table_schema = 'public' AND table_name = 'claim_embeddings'
                    ) AS ok
                    """
            )
            row = cur.fetchone()
        self._vector_table_available = bool(row["ok"]) if row is not None else False
        return self._vector_table_available

    @staticmethod
    def _vector_literal(vec: list[float]) -> str:
        return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"

    def upsert_embeddings(self, claims: list[Claim], provider: EmbeddingProvider) -> int:
        if not claims:
            return 0
        if not self._has_vector_table():
            return 0
        now = utc_now()
        rows: list[tuple[object, ...]] = []
        for claim in claims:
            text = " ".join(
                part
                for part in [claim.text, claim.normalized_text or "", claim.subject or "", claim.object_value or ""]
                if part
            )
            emb = provider.embed(text)
            rows.append((claim.id, provider.model, self._vector_literal(emb), now))

        with self.connect() as conn, conn.cursor() as cur:
            cur.executemany(
                """
                    INSERT INTO claim_embeddings (claim_id, model, embedding, updated_at)
                    VALUES (%s, %s, %s::vector, %s)
                    ON CONFLICT (claim_id) DO UPDATE SET
                      model = EXCLUDED.model,
                      embedding = EXCLUDED.embedding,
                      updated_at = EXCLUDED.updated_at
                    """,
                rows,
            )
        return len(rows)

    def vector_scores(
        self,
        query_text: str,
        claims: list[Claim],
        provider: EmbeddingProvider,
    ) -> dict[int, float]:
        if not claims:
            return {}
        if not self._has_vector_table():
            return self._vector_scores_fallback(query_text, claims, provider)

        self.upsert_embeddings(claims, provider)
        query_vec = self._vector_literal(provider.embed(query_text))
        ids = [c.id for c in claims]
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT claim_id, 1 - (embedding <=> %s::vector) AS sim
                    FROM claim_embeddings
                    WHERE claim_id = ANY(%s)
                    """,
                (query_vec, ids),
            )
            rows = cur.fetchall()
        out: dict[int, float] = {}
        for row in rows:
            sim = float(row["sim"])
            out[int(row["claim_id"])] = max(0.0, min(1.0, (sim + 1.0) / 2.0))
        return out

    def _vector_scores_fallback(
        self,
        query_text: str,
        claims: list[Claim],
        provider: EmbeddingProvider,
    ) -> dict[int, float]:
        query_vec = provider.embed(query_text)
        out: dict[int, float] = {}
        for claim in claims:
            text = " ".join(
                part
                for part in [claim.text, claim.normalized_text or "", claim.subject or "", claim.object_value or ""]
                if part
            )
            emb = provider.embed(text)
            sim = cosine_similarity(query_vec, emb)
            out[claim.id] = max(0.0, min(1.0, (sim + 1.0) / 2.0))
        return out

    @staticmethod
    def _as_iso(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(microsecond=0).isoformat()
        return str(value)

    @staticmethod
    def _as_text(value: object) -> str | None:
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _handle_dollar_quote(
        sql: str,
        i: int,
        current: list[str],
        dollar_quote_tag: str | None,
    ) -> tuple[int, str | None]:
        """Handle dollar-quoted string transitions."""
        if dollar_quote_tag is not None:
            if sql.startswith(dollar_quote_tag, i):
                current.append(dollar_quote_tag)
                return i + len(dollar_quote_tag), None
            current.append(sql[i])
            return i + 1, dollar_quote_tag

        # Check if starting a new dollar quote
        if sql[i] == "$":
            j = i + 1
            while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < len(sql) and sql[j] == "$":
                tag = sql[i : j + 1]
                current.append(tag)
                return j + 1, tag

        return i, dollar_quote_tag

    @staticmethod
    def _handle_single_quote(
        sql: str,
        i: int,
        current: list[str],
        in_single_quote: bool,
    ) -> tuple[int, bool]:
        """Handle single-quoted string transitions."""
        if in_single_quote:
            current.append(sql[i])
            if sql[i] == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    current.append("'")
                    return i + 2, True
                return i + 1, False
            return i + 1, True

        if sql[i] == "'":
            current.append(sql[i])
            return i + 1, True

        return i, in_single_quote

    @staticmethod
    def _split_sql_statements(sql: str) -> list[str]:
        statements: list[str] = []
        current: list[str] = []
        in_single_quote = False
        dollar_quote_tag: str | None = None
        i = 0
        n = len(sql)

        while i < n:
            ch = sql[i]

            # Handle dollar-quoted strings
            new_i, new_tag = PostgresStore._handle_dollar_quote(sql, i, current, dollar_quote_tag)
            if new_i != i:
                i = new_i
                dollar_quote_tag = new_tag
                continue

            # Handle single-quoted strings
            new_i, new_in_quote = PostgresStore._handle_single_quote(sql, i, current, in_single_quote)
            if new_i != i or new_in_quote != in_single_quote:
                i = new_i
                in_single_quote = new_in_quote
                continue

            # Handle statement terminator
            if ch == ";":
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
                i += 1
                continue

            current.append(ch)
            i += 1

        tail = "".join(current).strip()
        if tail:
            statements.append(tail)
        return statements

    @classmethod
    def _row_to_claim(cls, row: Any) -> Claim:
        return Claim(
            id=int(row["id"]),
            text=str(row["text"]),
            idempotency_key=cls._as_text(row.get("idempotency_key")),
            normalized_text=cls._as_text(row["normalized_text"]),
            claim_type=cls._as_text(row["claim_type"]),
            subject=cls._as_text(row["subject"]),
            predicate=cls._as_text(row["predicate"]),
            object_value=cls._as_text(row["object_value"]),
            scope=str(row["scope"]),
            volatility=str(row["volatility"]),
            status=str(row["status"]),
            confidence=float(row["confidence"]),
            pinned=bool(row["pinned"]),
            supersedes_claim_id=int(row["supersedes_claim_id"]) if row["supersedes_claim_id"] is not None else None,
            replaced_by_claim_id=int(row["replaced_by_claim_id"]) if row["replaced_by_claim_id"] is not None else None,
            created_at=cls._as_iso(row["created_at"]) or "",
            updated_at=cls._as_iso(row["updated_at"]) or "",
            last_validated_at=cls._as_iso(row["last_validated_at"]),
            archived_at=cls._as_iso(row["archived_at"]),
            human_id=cls._as_text(row.get("human_id")),
            tenant_id=cls._as_text(row.get("tenant_id")),
            tier=cls._as_text(row.get("tier")) or "working",
            access_count=int(row.get("access_count") or 0),
            last_accessed=cls._as_iso(row.get("last_accessed")),
            event_time=cls._as_iso(row.get("event_time")),
            valid_from=cls._as_iso(row.get("valid_from")),
            valid_until=cls._as_iso(row.get("valid_until")),
            source_agent=cls._as_text(row.get("source_agent")),
            visibility=cls._as_text(row.get("visibility")) or "public",
            wiki_article=cls._as_text(row.get("wiki_article")),
            holder=cls._as_text(row.get("holder")),
        )

    @classmethod
    def _row_to_citation(cls, row: Any) -> Citation:
        return Citation(
            id=int(row["id"]),
            claim_id=int(row["claim_id"]),
            source=str(row["source"]),
            locator=cls._as_text(row["locator"]),
            excerpt=cls._as_text(row["excerpt"]),
            created_at=cls._as_iso(row["created_at"]) or "",
        )

    @classmethod
    def _row_to_event(cls, row: Any) -> Event:
        payload_value = row["payload_json"]
        if payload_value is None:
            payload_json = None
        elif isinstance(payload_value, str):
            payload_json = payload_value
        else:
            payload_json = json.dumps(payload_value)

        return Event(
            id=int(row["id"]),
            claim_id=int(row["claim_id"]) if row["claim_id"] is not None else None,
            event_type=str(row["event_type"]),
            from_status=cls._as_text(row["from_status"]),
            to_status=cls._as_text(row["to_status"]),
            details=cls._as_text(row["details"]),
            payload_json=payload_json,
            created_at=cls._as_iso(row["created_at"]) or "",
        )

    @classmethod
    def _json_text(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def _row_to_external_source(cls, row: Any) -> ExternalSource:
        return ExternalSource(
            id=int(row["id"]),
            source_type=str(row["source_type"]),
            display_name=str(row["display_name"]),
            config_json=cls._json_text(row.get("config_json")),
            created_at=cls._as_iso(row["created_at"]) or "",
            updated_at=cls._as_iso(row["updated_at"]) or "",
        )

    @classmethod
    def _row_to_source_item(cls, row: Any) -> SourceItem:
        return SourceItem(
            id=int(row["id"]),
            source_id=int(row["source_id"]),
            source_item_id=str(row["source_item_id"]),
            item_type=str(row["item_type"]),
            chat_id=cls._as_text(row.get("chat_id")),
            sender_id=cls._as_text(row.get("sender_id")),
            sender_name=cls._as_text(row.get("sender_name")),
            occurred_at=cls._as_iso(row.get("occurred_at")),
            text=cls._as_text(row.get("text")),
            payload_json=cls._json_text(row.get("payload_json")),
            content_hash=cls._as_text(row.get("content_hash")),
            sensitivity=cls._as_text(row.get("sensitivity")),
            created_at=cls._as_iso(row["created_at"]) or "",
            updated_at=cls._as_iso(row["updated_at"]) or "",
        )

    @classmethod
    def _row_to_evidence_item(cls, row: Any) -> EvidenceItem:
        confidence = row.get("confidence")
        return EvidenceItem(
            id=int(row["id"]),
            source_item_id=int(row["source_item_id"]),
            evidence_type=str(row["evidence_type"]),
            text=cls._as_text(row.get("text")),
            media_path=cls._as_text(row.get("media_path")),
            provider=cls._as_text(row.get("provider")),
            confidence=float(confidence) if confidence is not None else None,
            payload_json=cls._json_text(row.get("payload_json")),
            sensitivity=cls._as_text(row.get("sensitivity")),
            created_at=cls._as_iso(row["created_at"]) or "",
        )

    @classmethod
    def _row_to_action_proposal(cls, row: Any) -> ActionProposal:
        return ActionProposal(
            id=int(row["id"]),
            proposal_type=str(row["proposal_type"]),
            title=str(row["title"]),
            description=cls._as_text(row.get("description")),
            source_item_id=int(row["source_item_id"]) if row.get("source_item_id") is not None else None,
            evidence_item_id=int(row["evidence_item_id"]) if row.get("evidence_item_id") is not None else None,
            claim_id=int(row["claim_id"]) if row.get("claim_id") is not None else None,
            suggested_due_at=cls._as_iso(row.get("suggested_due_at")),
            destination=str(row["destination"]),
            status=str(row["status"]),
            confidence=float(row["confidence"]),
            payload_json=cls._json_text(row.get("payload_json")),
            exported_at=cls._as_iso(row.get("exported_at")),
            external_ref=cls._as_text(row.get("external_ref")),
            idempotency_key=cls._as_text(row.get("idempotency_key")),
            created_at=cls._as_iso(row["created_at"]) or "",
            updated_at=cls._as_iso(row["updated_at"]) or "",
        )

    @classmethod
    def _row_to_claim_link(cls, row: Any) -> ClaimLink:
        return ClaimLink(
            id=int(row["id"]),
            source_id=int(row["source_id"]),
            target_id=int(row["target_id"]),
            link_type=str(row["link_type"]),
            created_at=cls._as_iso(row["created_at"]) or "",
        )

    @staticmethod
    def _ensure_claim_links_schema(conn) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS claim_links (
                    id BIGSERIAL PRIMARY KEY,
                    source_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
                    target_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
                    link_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    CHECK (source_id <> target_id),
                    CHECK (link_type IN ('relates_to', 'supersedes', 'derived_from', 'contradicts', 'supports'))
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_links_unique ON claim_links(source_id, target_id, link_type)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_claim_links_source ON claim_links(source_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_claim_links_target ON claim_links(target_id)"
            )

    @staticmethod
    def _ensure_human_id_schema(conn) -> None:
        """Add human_id column if missing and backfill existing claims."""
        PostgresStore._ensure_tenant_id_schema(conn)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE claims ADD COLUMN IF NOT EXISTS human_id TEXT")
            cur.execute(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM pg_class idx
                        JOIN pg_index meta ON meta.indexrelid = idx.oid
                        WHERE idx.relname = 'idx_claims_human_id'
                          AND meta.indisunique
                    ) THEN
                        DROP INDEX idx_claims_human_id;
                    END IF;
                END
                $$
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_claims_human_id ON claims(human_id)"
            )
            cur.execute(
                "DROP INDEX IF EXISTS idx_claims_tenant_human_id"
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_public_human_id_unique
                ON claims(COALESCE(tenant_id, ''), scope, human_id)
                WHERE visibility = 'public' AND human_id IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_nonpublic_principal_human_id_unique
                ON claims(COALESCE(tenant_id, ''), scope, visibility, source_agent, human_id)
                WHERE visibility <> 'public' AND source_agent IS NOT NULL
                  AND human_id IS NOT NULL
                """
            )
        PostgresStore._backfill_human_ids(conn)

    @staticmethod
    def _backfill_human_ids(conn) -> int:
        """Assign human_id to all claims that lack one."""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, subject, text, tenant_id, scope, visibility, source_agent
                FROM claims WHERE human_id IS NULL ORDER BY id ASC
                """
            )
            rows = cur.fetchall()
            if not rows:
                return 0
            updated = 0
            for row in rows:
                claim_id = int(row["id"])
                subject = PostgresStore._as_text(row["subject"])
                text = str(row["text"])
                human_id = PostgresStore._allocate_human_id(
                    cur,
                    subject,
                    text,
                    claim_id,
                    tenant_id=row.get("tenant_id"),
                    scope=PostgresStore._as_text(row.get("scope")) or "project",
                    visibility=PostgresStore._as_text(row.get("visibility")) or "public",
                    source_agent=PostgresStore._as_text(row.get("source_agent")),
                )
                cur.execute(
                    "UPDATE claims SET human_id = %s WHERE id = %s",
                    (human_id, claim_id),
                )
                updated += 1
            return updated

    @staticmethod
    def _allocate_human_id(
        cur,
        subject: str | None,
        text: str,
        claim_id: int,
        tenant_id: str | None = None,
        scope: str = "project",
        visibility: str = "public",
        source_agent: str | None = None,
    ) -> str:
        """Build a unique human_id, checking for derived_from parent links."""
        identity_sql, identity_params = PostgresStore._postgres_identity_filter(
            visibility,
            source_agent,
            alias="c",
        )
        cur.execute(
            f"""
            SELECT c.human_id
            FROM claim_links cl
            JOIN claims c ON c.id = cl.target_id
            WHERE cl.source_id = %s
              AND cl.link_type = 'derived_from'
              AND c.human_id IS NOT NULL
              AND c.tenant_id IS NOT DISTINCT FROM %s
              AND c.scope = %s
              AND {identity_sql}
            LIMIT 1
            """,
            (claim_id, tenant_id, scope, *identity_params),
        )
        parent_row = cur.fetchone()

        if parent_row and parent_row["human_id"]:
            parent_hid = str(parent_row["human_id"])
            cur.execute(
                f"""
                SELECT COUNT(*) AS cnt FROM claims
                WHERE human_id LIKE %s AND human_id != %s
                  AND tenant_id IS NOT DISTINCT FROM %s
                  AND scope = %s
                  AND {identity_sql.replace('c.', '')}
                """,
                (parent_hid + ".%", parent_hid, tenant_id, scope, *identity_params),
            )
            child_count = cur.fetchone()
            next_child = (int(child_count["cnt"]) if child_count else 0) + 1
            candidate = f"{parent_hid}.{next_child}"
        else:
            candidate = generate_top_level_human_id(subject, text)

        final = candidate
        suffix = 1
        while True:
            cur.execute(
                f"""
                SELECT 1 FROM claims
                WHERE human_id = %s AND tenant_id IS NOT DISTINCT FROM %s
                  AND scope = %s
                  AND {identity_sql.replace('c.', '')}
                """,
                (final, tenant_id, scope, *identity_params),
            )
            existing = cur.fetchone()
            if existing is None:
                return final
            suffix += 1
            final = f"{candidate}~{suffix}"

    @staticmethod
    def _ensure_tenant_id_schema(conn) -> None:
        """Add tenant_id column if missing, with an index for tenant isolation."""
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE claims ADD COLUMN IF NOT EXISTS tenant_id TEXT")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_claims_tenant_id ON claims(tenant_id)"
            )

    def _ensure_binding_schema(self, conn) -> None:
        """Add wiki_article column for claim↔wiki bidirectional binding (v3.4)."""
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE claims ADD COLUMN IF NOT EXISTS wiki_article TEXT")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_claims_wiki_article ON claims(wiki_article)"
            )

    @staticmethod
    def _json_payload(value: dict[str, object] | str | None) -> object | None:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return stripped
        return value

    def upsert_external_source(
        self,
        *,
        source_type: str,
        display_name: str,
        config_json: dict[str, object] | str | None = None,
    ) -> ExternalSource:
        self._deny_unsupported_team_surface("upsert_external_source")
        _, _, Jsonb = self._load_psycopg()
        normalized_source_type = source_type.strip().lower()
        normalized_display_name = display_name.strip()
        if not normalized_source_type:
            raise ValueError("source_type must be non-empty.")
        if not normalized_display_name:
            raise ValueError("display_name must be non-empty.")
        now = utc_now()
        payload = self._json_payload(config_json)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO external_sources (source_type, display_name, config_json, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(source_type, display_name) DO UPDATE SET
                    config_json = EXCLUDED.config_json,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    normalized_source_type,
                    normalized_display_name,
                    Jsonb(payload) if payload is not None else None,
                    now,
                    now,
                ),
            )
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("Failed to upsert external source.")
        return self._row_to_external_source(row)

    def upsert_source_item(
        self,
        *,
        source_id: int,
        source_item_id: str,
        item_type: str,
        chat_id: str | None = None,
        sender_id: str | None = None,
        sender_name: str | None = None,
        occurred_at: str | None = None,
        text: str | None = None,
        payload_json: dict[str, object] | str | None = None,
        content_hash: str | None = None,
        sensitivity: str | None = None,
    ) -> SourceItem:
        self._deny_unsupported_team_surface("upsert_source_item")
        from memorymaster.stores._storage_sources import _normalize_sensitivity

        _, _, Jsonb = self._load_psycopg()
        normalized_source_item_id = source_item_id.strip()
        normalized_item_type = item_type.strip().lower()
        if source_id <= 0:
            raise ValueError("source_id must be positive.")
        if not normalized_source_item_id:
            raise ValueError("source_item_id must be non-empty.")
        if not normalized_item_type:
            raise ValueError("item_type must be non-empty.")
        normalized_sensitivity = _normalize_sensitivity(sensitivity)
        now = utc_now()
        payload = self._json_payload(payload_json)
        # Preserve existing sensitivity on re-import unless caller passed one
        preserve_sensitivity_clause = (
            "sensitivity = EXCLUDED.sensitivity"
            if sensitivity is not None
            else "sensitivity = source_items.sensitivity"
        )
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM source_items WHERE source_id = %s AND source_item_id = %s",
                (source_id, normalized_source_item_id),
            )
            existing = cur.fetchone()
            cur.execute(
                f"""
                INSERT INTO source_items (
                    source_id, source_item_id, item_type, chat_id, sender_id, sender_name,
                    occurred_at, text, payload_json, content_hash, sensitivity, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(source_id, source_item_id) DO UPDATE SET
                    item_type = EXCLUDED.item_type,
                    chat_id = EXCLUDED.chat_id,
                    sender_id = EXCLUDED.sender_id,
                    sender_name = EXCLUDED.sender_name,
                    occurred_at = EXCLUDED.occurred_at,
                    text = EXCLUDED.text,
                    payload_json = EXCLUDED.payload_json,
                    content_hash = EXCLUDED.content_hash,
                    {preserve_sensitivity_clause},
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    source_id,
                    normalized_source_item_id,
                    normalized_item_type,
                    chat_id,
                    sender_id,
                    sender_name,
                    occurred_at,
                    text,
                    Jsonb(payload) if payload is not None else None,
                    content_hash,
                    normalized_sensitivity,
                    now,
                    now,
                ),
            )
            row = cur.fetchone()
            if existing is None:
                self._insert_event_row(
                    conn,
                    claim_id=None,
                    event_type="source_import",
                    from_status=None,
                    to_status=None,
                    details="source_item_imported",
                    payload={
                        "source_id": source_id,
                        "source_item_id": normalized_source_item_id,
                        "item_type": normalized_item_type,
                    },
                    created_at=now,
                )
        if row is None:
            raise RuntimeError("Failed to upsert source item.")
        return self._row_to_source_item(row)

    def get_source_item(self, *, source_id: int, source_item_id: str) -> SourceItem | None:
        self._deny_unsupported_team_surface("get_source_item")
        normalized_source_item_id = source_item_id.strip()
        if source_id <= 0:
            raise ValueError("source_id must be positive.")
        if not normalized_source_item_id:
            return None
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM source_items WHERE source_id = %s AND source_item_id = %s",
                (source_id, normalized_source_item_id),
            )
            row = cur.fetchone()
        return self._row_to_source_item(row) if row is not None else None

    def get_source_item_by_id(self, source_item_row_id: int) -> SourceItem | None:
        self._deny_unsupported_team_surface("get_source_item_by_id")
        if source_item_row_id <= 0:
            raise ValueError("source_item_row_id must be positive.")
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM source_items WHERE id = %s", (source_item_row_id,))
            row = cur.fetchone()
        return self._row_to_source_item(row) if row is not None else None

    def add_evidence_item(
        self,
        *,
        source_item_id: int,
        evidence_type: str,
        text: str | None = None,
        media_path: str | None = None,
        provider: str | None = None,
        confidence: float | None = None,
        payload_json: dict[str, object] | str | None = None,
        sensitivity: str | None = None,
    ) -> EvidenceItem:
        self._deny_unsupported_team_surface("add_evidence_item")
        from memorymaster.stores._storage_sources import _normalize_sensitivity

        _, _, Jsonb = self._load_psycopg()
        normalized_evidence_type = evidence_type.strip().lower()
        if source_item_id <= 0:
            raise ValueError("source_item_id must be positive.")
        if not normalized_evidence_type:
            raise ValueError("evidence_type must be non-empty.")
        normalized_sensitivity = _normalize_sensitivity(sensitivity)
        now = utc_now()
        bounded = None if confidence is None else max(0.0, min(1.0, float(confidence)))
        payload = self._json_payload(payload_json)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO evidence_items (
                    source_item_id, evidence_type, text, media_path, provider,
                    confidence, payload_json, sensitivity, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    source_item_id,
                    normalized_evidence_type,
                    text,
                    media_path,
                    provider,
                    bounded,
                    Jsonb(payload) if payload is not None else None,
                    normalized_sensitivity,
                    now,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("Failed to add evidence item.")
            self._insert_event_row(
                conn,
                claim_id=None,
                event_type="media_process",
                from_status=None,
                to_status=None,
                details="evidence_item_added",
                payload={
                    "source_item_id": source_item_id,
                    "evidence_item_id": int(row["id"]),
                    "evidence_type": normalized_evidence_type,
                },
                created_at=now,
            )
        return self._row_to_evidence_item(row)

    def list_evidence_items(
        self,
        *,
        source_item_id: int | None = None,
        evidence_type: str | None = None,
        limit: int = 100,
    ) -> list[EvidenceItem]:
        self._deny_unsupported_team_surface("list_evidence_items")
        clauses: list[str] = []
        params: list[object] = []
        if source_item_id is not None:
            if source_item_id <= 0:
                raise ValueError("source_item_id must be positive.")
            clauses.append("source_item_id = %s")
            params.append(source_item_id)
        if evidence_type:
            clauses.append("evidence_type = %s")
            params.append(evidence_type.strip().lower())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM evidence_items {where_sql} ORDER BY created_at ASC, id ASC LIMIT %s",
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_evidence_item(row) for row in rows]

    def create_action_proposal(
        self,
        *,
        proposal_type: str,
        title: str,
        description: str | None = None,
        source_item_id: int | None = None,
        evidence_item_id: int | None = None,
        claim_id: int | None = None,
        suggested_due_at: str | None = None,
        destination: str = "manual",
        confidence: float = 0.5,
        payload_json: dict[str, object] | str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionProposal:
        self._deny_unsupported_team_surface("create_action_proposal")
        _, _, Jsonb = self._load_psycopg()
        normalized_type = proposal_type.strip().lower()
        normalized_title = title.strip()
        normalized_destination = destination.strip() or "manual"
        normalized_idempotency_key = (idempotency_key or "").strip() or None
        if not normalized_type:
            raise ValueError("proposal_type must be non-empty.")
        if not normalized_title:
            raise ValueError("title must be non-empty.")
        if normalized_idempotency_key:
            existing = self.get_action_proposal_by_idempotency_key(normalized_idempotency_key)
            if existing is not None:
                return existing
        now = utc_now()
        bounded = max(0.0, min(1.0, float(confidence)))
        payload = self._json_payload(payload_json)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO action_proposals (
                    proposal_type, title, description, source_item_id, evidence_item_id,
                    claim_id, suggested_due_at, destination, status, confidence, payload_json,
                    exported_at, external_ref, idempotency_key, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'candidate', %s, %s, NULL, NULL, %s, %s, %s)
                RETURNING *
                """,
                (
                    normalized_type,
                    normalized_title,
                    description,
                    source_item_id,
                    evidence_item_id,
                    claim_id,
                    suggested_due_at,
                    normalized_destination,
                    bounded,
                    Jsonb(payload) if payload is not None else None,
                    normalized_idempotency_key,
                    now,
                    now,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("Failed to create action proposal.")
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="action_proposal",
                from_status=None,
                to_status="candidate",
                details="action_proposal_created",
                payload={
                    "proposal_id": int(row["id"]),
                    "proposal_type": normalized_type,
                    "destination": normalized_destination,
                },
                created_at=now,
            )
        return self._row_to_action_proposal(row)

    def get_action_proposal_by_idempotency_key(self, idempotency_key: str) -> ActionProposal | None:
        self._deny_unsupported_team_surface("get_action_proposal_by_idempotency_key")
        normalized = idempotency_key.strip()
        if not normalized:
            return None
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM action_proposals WHERE idempotency_key = %s", (normalized,))
            row = cur.fetchone()
        return self._row_to_action_proposal(row) if row is not None else None

    def update_action_proposal_status(
        self,
        proposal_id: int,
        *,
        status: str,
        external_ref: str | None = None,
        exported_at: str | None = None,
        payload_json: dict[str, object] | str | None = None,
    ) -> ActionProposal:
        self._deny_unsupported_team_surface("update_action_proposal_status")
        _, _, Jsonb = self._load_psycopg()
        normalized_status = status.strip().lower()
        if proposal_id <= 0:
            raise ValueError("proposal_id must be positive.")
        if normalized_status not in {"candidate", "approved", "rejected", "exported", "failed"}:
            raise ValueError("status must be one of: candidate, approved, rejected, exported, failed.")
        now = utc_now()
        payload = self._json_payload(payload_json)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM action_proposals WHERE id = %s", (proposal_id,))
            current = cur.fetchone()
            if current is None:
                raise ValueError(f"Action proposal {proposal_id} does not exist.")
            final_exported_at = exported_at if exported_at is not None else current["exported_at"]
            if normalized_status == "exported" and final_exported_at is None:
                final_exported_at = now
            if payload is not None:
                final_payload = Jsonb(payload)
            elif current["payload_json"] is not None:
                final_payload = Jsonb(current["payload_json"])
            else:
                final_payload = None
            final_external_ref = external_ref if external_ref is not None else current["external_ref"]
            cur.execute(
                """
                UPDATE action_proposals
                SET status = %s,
                    external_ref = %s,
                    exported_at = %s,
                    payload_json = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (normalized_status, final_external_ref, final_exported_at, final_payload, now, proposal_id),
            )
            row = cur.fetchone()
            event_type = "action_export" if normalized_status == "exported" else "action_proposal"
            self._insert_event_row(
                conn,
                claim_id=int(current["claim_id"]) if current["claim_id"] is not None else None,
                event_type=event_type,
                from_status=str(current["status"]),
                to_status=normalized_status,
                details="action_proposal_status_updated",
                payload={"proposal_id": proposal_id, "status": normalized_status},
                created_at=now,
            )
        if row is None:
            raise RuntimeError("Failed to update action proposal.")
        return self._row_to_action_proposal(row)

    def set_source_item_sensitivity(
        self,
        source_item_row_id: int,
        sensitivity: str | None,
    ) -> SourceItem:
        self._deny_unsupported_team_surface("set_source_item_sensitivity")
        from memorymaster.stores._storage_sources import _normalize_sensitivity

        if source_item_row_id <= 0:
            raise ValueError("source_item_row_id must be positive.")
        normalized = _normalize_sensitivity(sensitivity)
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM source_items WHERE id = %s", (source_item_row_id,))
            current = cur.fetchone()
            if current is None:
                raise ValueError(f"Source item {source_item_row_id} does not exist.")
            current_sensitivity = current.get("sensitivity")
            if current_sensitivity == normalized:
                return self._row_to_source_item(current)
            cur.execute(
                "UPDATE source_items SET sensitivity = %s, updated_at = %s WHERE id = %s RETURNING *",
                (normalized, now, source_item_row_id),
            )
            row = cur.fetchone()
            self._insert_event_row(
                conn,
                claim_id=None,
                event_type="source_import",
                from_status=None,
                to_status=None,
                details="source_item_sensitivity_set",
                payload={"source_item_id": source_item_row_id, "from": current_sensitivity, "to": normalized},
                created_at=now,
            )
        return self._row_to_source_item(row)

    def set_evidence_item_sensitivity(
        self,
        evidence_item_row_id: int,
        sensitivity: str | None,
    ) -> EvidenceItem:
        self._deny_unsupported_team_surface("set_evidence_item_sensitivity")
        from memorymaster.stores._storage_sources import _normalize_sensitivity

        if evidence_item_row_id <= 0:
            raise ValueError("evidence_item_row_id must be positive.")
        normalized = _normalize_sensitivity(sensitivity)
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM evidence_items WHERE id = %s", (evidence_item_row_id,))
            current = cur.fetchone()
            if current is None:
                raise ValueError(f"Evidence item {evidence_item_row_id} does not exist.")
            current_sensitivity = current.get("sensitivity")
            if current_sensitivity == normalized:
                return self._row_to_evidence_item(current)
            cur.execute(
                "UPDATE evidence_items SET sensitivity = %s WHERE id = %s RETURNING *",
                (normalized, evidence_item_row_id),
            )
            row = cur.fetchone()
            self._insert_event_row(
                conn,
                claim_id=None,
                event_type="media_process",
                from_status=None,
                to_status=None,
                details="evidence_item_sensitivity_set",
                payload={"evidence_item_id": evidence_item_row_id, "from": current_sensitivity, "to": normalized},
                created_at=now,
            )
        return self._row_to_evidence_item(row)

    # ----------------------------------------------------------------------
    # Media retry queue (Atlas v1.4.0) — Postgres mirror of SQLite mixin
    # ----------------------------------------------------------------------

    @classmethod
    def _row_to_media_retry(cls, row: Any) -> MediaRetryItem:
        return MediaRetryItem(
            id=int(row["id"]),
            source_item_id=int(row["source_item_id"]),
            media_key=str(row["media_key"]),
            chat_id=cls._as_text(row.get("chat_id")),
            media_type=cls._as_text(row.get("media_type")),
            media_path=cls._as_text(row.get("media_path")),
            media_url=cls._as_text(row.get("media_url")),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            last_http_status=int(row["last_http_status"]) if row.get("last_http_status") is not None else None,
            last_error=cls._as_text(row.get("last_error")),
            next_attempt_time=cls._as_iso(row.get("next_attempt_time")),
            created_at=cls._as_iso(row["created_at"]) or "",
            updated_at=cls._as_iso(row["updated_at"]) or "",
        )

    def enqueue_media_retry(
        self,
        *,
        source_item_id: int,
        media_key: str,
        chat_id: str | None = None,
        media_type: str | None = None,
        media_path: str | None = None,
        media_url: str | None = None,
        status: str = "pending",
        next_attempt_time: str | None = None,
    ) -> MediaRetryItem:
        self._deny_unsupported_team_surface("enqueue_media_retry")
        if source_item_id <= 0:
            raise ValueError("source_item_id must be positive.")
        normalized_key = (media_key or "").strip()
        if not normalized_key:
            raise ValueError("media_key must be non-empty.")
        if status not in MEDIA_RETRY_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(MEDIA_RETRY_STATUSES)}.")
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM media_retry_queue WHERE source_item_id = %s AND media_key = %s",
                (source_item_id, normalized_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                cur.execute(
                    """
                    UPDATE media_retry_queue
                    SET chat_id = COALESCE(%s, chat_id),
                        media_type = COALESCE(%s, media_type),
                        media_path = COALESCE(%s, media_path),
                        media_url = COALESCE(%s, media_url),
                        next_attempt_time = COALESCE(%s, next_attempt_time),
                        updated_at = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (chat_id, media_type, media_path, media_url, next_attempt_time, now, int(existing["id"])),
                )
                row = cur.fetchone()
                return self._row_to_media_retry(row)
            cur.execute(
                """
                INSERT INTO media_retry_queue (
                    source_item_id, media_key, chat_id, media_type, media_path, media_url,
                    status, attempt_count, last_http_status, last_error, next_attempt_time,
                    created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 0, NULL, NULL, %s, %s, %s)
                RETURNING *
                """,
                (
                    source_item_id, normalized_key, chat_id, media_type, media_path, media_url,
                    status, next_attempt_time, now, now,
                ),
            )
            row = cur.fetchone()
            self._insert_event_row(
                conn,
                claim_id=None,
                event_type="media_process",
                from_status=None,
                to_status=status,
                details="media_retry_enqueued",
                payload={"retry_id": int(row["id"]), "source_item_id": source_item_id, "media_key": normalized_key},
                created_at=now,
            )
        return self._row_to_media_retry(row)

    def claim_pending_media_retries(self, limit: int = 25) -> list[MediaRetryItem]:
        self._deny_unsupported_team_surface("claim_pending_media_retries")
        if limit <= 0:
            return []
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE media_retry_queue
                SET status = 'retrying',
                    attempt_count = attempt_count + 1,
                    updated_at = %s
                WHERE id IN (
                    SELECT id FROM media_retry_queue
                    WHERE status = 'pending'
                      AND (next_attempt_time IS NULL OR next_attempt_time <= %s)
                    ORDER BY id ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                (now, now, limit),
            )
            rows = cur.fetchall()
            for row in rows:
                self._insert_event_row(
                    conn,
                    claim_id=None,
                    event_type="media_process",
                    from_status="pending",
                    to_status="retrying",
                    details="media_retry_claimed",
                    payload={"retry_id": int(row["id"])},
                    created_at=now,
                )
        return [self._row_to_media_retry(r) for r in rows]

    def record_media_retry_outcome(
        self,
        retry_id: int,
        *,
        status: str,
        media_path: str | None = None,
        last_http_status: int | None = None,
        last_error: str | None = None,
        next_attempt_time: str | None = None,
    ) -> MediaRetryItem:
        self._deny_unsupported_team_surface("record_media_retry_outcome")
        if retry_id <= 0:
            raise ValueError("retry_id must be positive.")
        if status not in MEDIA_RETRY_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(MEDIA_RETRY_STATUSES)}.")
        if status == "done" and not media_path:
            raise ValueError("media_path is required when status='done'.")
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM media_retry_queue WHERE id = %s", (retry_id,))
            current = cur.fetchone()
            if current is None:
                raise ValueError(f"media_retry_queue row {retry_id} does not exist.")
            new_path = media_path if media_path is not None else current["media_path"]
            new_http = last_http_status if last_http_status is not None else current["last_http_status"]
            new_err = last_error if last_error is not None else current["last_error"]
            new_next = next_attempt_time if next_attempt_time is not None else current["next_attempt_time"]
            cur.execute(
                """
                UPDATE media_retry_queue
                SET status = %s,
                    media_path = %s,
                    last_http_status = %s,
                    last_error = %s,
                    next_attempt_time = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (status, new_path, new_http, new_err, new_next, now, retry_id),
            )
            row = cur.fetchone()
            self._insert_event_row(
                conn,
                claim_id=None,
                event_type="media_process",
                from_status=str(current["status"]),
                to_status=status,
                details=f"media_retry_outcome_{status}",
                payload={
                    "retry_id": retry_id,
                    "http_status": last_http_status,
                    "has_path": bool(new_path),
                },
                created_at=now,
            )
        return self._row_to_media_retry(row)

    def list_media_retries(
        self,
        *,
        status: str | None = None,
        source_item_id: int | None = None,
        limit: int = 100,
    ) -> list[MediaRetryItem]:
        self._deny_unsupported_team_surface("list_media_retries")
        clauses: list[str] = []
        params: list[object] = []
        if status:
            if status not in MEDIA_RETRY_STATUSES:
                raise ValueError(f"status must be one of: {', '.join(MEDIA_RETRY_STATUSES)}.")
            clauses.append("status = %s")
            params.append(status)
        if source_item_id is not None:
            if source_item_id <= 0:
                raise ValueError("source_item_id must be positive.")
            clauses.append("source_item_id = %s")
            params.append(source_item_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM media_retry_queue {where_sql} ORDER BY updated_at DESC, id DESC LIMIT %s",
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_media_retry(r) for r in rows]

    def media_retry_status_counts(self) -> dict[str, int]:
        self._deny_unsupported_team_surface("media_retry_status_counts")
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) AS n FROM media_retry_queue GROUP BY status")
            rows = cur.fetchall()
        counts = {s: 0 for s in MEDIA_RETRY_STATUSES}
        for r in rows:
            counts[str(r["status"])] = int(r["n"])
        return counts

    def update_action_proposal_fields(
        self,
        proposal_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        suggested_due_at: str | None = None,
        confidence: float | None = None,
        payload_json: dict[str, object] | str | None = None,
    ) -> ActionProposal:
        """Postgres mirror of SQLite update_action_proposal_fields."""
        self._deny_unsupported_team_surface("update_action_proposal_fields")
        _, _, Jsonb = self._load_psycopg()
        if proposal_id <= 0:
            raise ValueError("proposal_id must be positive.")
        if title is None and description is None and suggested_due_at is None and confidence is None and payload_json is None:
            raise ValueError("at least one field must be provided to update.")

        normalized_title = title.strip() if title is not None else None
        if normalized_title is not None and not normalized_title:
            raise ValueError("title cannot be blank when provided.")
        bounded = max(0.0, min(1.0, float(confidence))) if confidence is not None else None
        payload = self._json_payload(payload_json) if payload_json is not None else None

        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM action_proposals WHERE id = %s", (proposal_id,))
            current = cur.fetchone()
            if current is None:
                raise ValueError(f"Action proposal {proposal_id} does not exist.")

            updates: list[str] = []
            params: list[object] = []
            changed: list[str] = []
            if normalized_title is not None and normalized_title != current["title"]:
                updates.append("title = %s")
                params.append(normalized_title)
                changed.append("title")
            if description is not None and description != current["description"]:
                updates.append("description = %s")
                params.append(description)
                changed.append("description")
            if suggested_due_at is not None and suggested_due_at != current["suggested_due_at"]:
                updates.append("suggested_due_at = %s")
                params.append(suggested_due_at)
                changed.append("suggested_due_at")
            if bounded is not None and bounded != current["confidence"]:
                updates.append("confidence = %s")
                params.append(bounded)
                changed.append("confidence")
            if payload is not None and payload != current["payload_json"]:
                updates.append("payload_json = %s")
                params.append(Jsonb(payload))
                changed.append("payload_json")

            if not changed:
                return self._row_to_action_proposal(current)

            updates.append("updated_at = %s")
            params.append(now)
            params.append(proposal_id)

            cur.execute(
                f"UPDATE action_proposals SET {', '.join(updates)} WHERE id = %s RETURNING *",
                params,
            )
            row = cur.fetchone()
            self._insert_event_row(
                conn,
                claim_id=int(current["claim_id"]) if current["claim_id"] is not None else None,
                event_type="action_proposal",
                from_status=str(current["status"]),
                to_status=str(current["status"]),
                details="action_proposal_fields_updated",
                payload={"proposal_id": proposal_id, "changed": changed},
                created_at=now,
            )
        if row is None:
            raise RuntimeError("Failed to update action proposal fields.")
        return self._row_to_action_proposal(row)

    def list_action_proposals(
        self,
        *,
        status: str | None = None,
        destination: str | None = None,
        limit: int = 100,
    ) -> list[ActionProposal]:
        self._deny_unsupported_team_surface("list_action_proposals")
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status = %s")
            params.append(status.strip().lower())
        if destination:
            clauses.append("destination = %s")
            params.append(destination.strip())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM action_proposals {where_sql} ORDER BY updated_at DESC, id DESC LIMIT %s",
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_action_proposal(row) for row in rows]

    def get_claim_by_human_id(
        self,
        human_id: str,
        include_citations: bool = True,
        *,
        tenant_id: str | None = None,
        scope: str | None = None,
        visibility: str = "public",
        source_agent: str | None = None,
    ) -> Claim | None:
        """Look up a claim by its human-readable ID (e.g. ``mm-a3f8``)."""
        normalized = human_id.strip()
        if not normalized:
            return None
        try:
            rows = self._select_claim_identity_rows(
                "human_id",
                normalized,
                tenant_id=tenant_id,
                scope=scope,
                visibility=visibility,
                source_agent=source_agent,
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "42703":
                return None
            raise
        return self._claim_from_identity_rows(
            rows,
            identifier="human_id",
            include_citations=include_citations,
        )

    def resolve_claim_id(
        self,
        identifier: str | int,
        *,
        tenant_id: str | None = None,
        scope: str | None = None,
        visibility: str = "public",
        source_agent: str | None = None,
    ) -> int:
        """Resolve a numeric ID or human_id string to a numeric claim ID."""
        if isinstance(identifier, int):
            return identifier
        raw = str(identifier).strip()
        try:
            return int(raw)
        except ValueError:
            pass
        claim = self.get_claim_by_human_id(
            raw,
            include_citations=False,
            tenant_id=tenant_id,
            scope=scope,
            visibility=visibility,
            source_agent=source_agent,
        )
        if claim is not None:
            return claim.id
        raise ValueError(f"No claim found for identifier '{raw}'.")

    def add_claim_link(self, source_id: int, target_id: int, link_type: str) -> ClaimLink:
        if link_type not in CLAIM_LINK_TYPES:
            allowed = ", ".join(CLAIM_LINK_TYPES)
            raise ValueError(f"Invalid link_type '{link_type}'. Allowed: {allowed}.")
        if source_id == target_id:
            raise ValueError("source_id and target_id must be different.")
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    """
                        INSERT INTO claim_links (source_id, target_id, link_type, created_at)
                        VALUES (%s, %s, %s, %s)
                        RETURNING id
                        """,
                    (source_id, target_id, link_type, now),
                )
                row = cur.fetchone()
            except Exception as exc:
                msg = str(exc).lower()
                if "unique" in msg or "duplicate key" in msg or "already exists" in msg:
                    raise ValueError(
                        f"Link already exists: {source_id} -> {target_id} ({link_type})."
                    ) from exc
                if "foreign key" in msg or "violates foreign key" in msg or "is not present" in msg:
                    raise ValueError(
                        f"One or both claim ids do not exist: {source_id}, {target_id}."
                    ) from exc
                if "check" in msg and "source_id" in msg:
                    raise ValueError("source_id and target_id must be different.") from exc
                raise
            if row is None:
                raise RuntimeError("Failed to insert claim link.")
            return ClaimLink(
                id=int(row["id"]),
                source_id=source_id,
                target_id=target_id,
                link_type=link_type,
                created_at=self._as_iso(now) or "",
            )

    def remove_claim_link(self, source_id: int, target_id: int, link_type: str | None = None) -> int:
        with self.connect() as conn, conn.cursor() as cur:
            if link_type is not None:
                cur.execute(
                    "DELETE FROM claim_links WHERE source_id = %s AND target_id = %s AND link_type = %s",
                    (source_id, target_id, link_type),
                )
            else:
                cur.execute(
                    "DELETE FROM claim_links WHERE source_id = %s AND target_id = %s",
                    (source_id, target_id),
                )
            return cur.rowcount

    def get_derived_from_target_ids(self, candidate_ids: list[int]) -> set[int]:
        """Return the subset of *candidate_ids* that are targets of a ``derived_from`` link."""
        if not candidate_ids:
            return set()
        with self.connect() as conn, conn.cursor() as cur:
            placeholders = ",".join("%s" for _ in candidate_ids)
            cur.execute(
                f"""
                    SELECT DISTINCT target_id FROM claim_links
                    WHERE link_type = 'derived_from'
                      AND target_id IN ({placeholders})
                    """,
                candidate_ids,
            )
            rows = cur.fetchall()
        return {row[0] if isinstance(row, (tuple, list)) else row["target_id"] for row in rows}

    def get_claim_links(self, claim_id: int) -> list[ClaimLink]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT * FROM claim_links
                    WHERE source_id = %s OR target_id = %s
                    ORDER BY created_at ASC
                    """,
                (claim_id, claim_id),
            )
            rows = cur.fetchall()
        return [self._row_to_claim_link(row) for row in rows]

    def get_linked_claims(self, claim_id: int, link_type: str | None = None) -> list[ClaimLink]:
        with self.connect() as conn, conn.cursor() as cur:
            if link_type is not None:
                cur.execute(
                    """
                        SELECT * FROM claim_links
                        WHERE (source_id = %s OR target_id = %s) AND link_type = %s
                        ORDER BY created_at ASC
                        """,
                    (claim_id, claim_id, link_type),
                )
            else:
                cur.execute(
                    """
                        SELECT * FROM claim_links
                        WHERE source_id = %s OR target_id = %s
                        ORDER BY created_at ASC
                        """,
                    (claim_id, claim_id),
                )
            rows = cur.fetchall()
        return [self._row_to_claim_link(row) for row in rows]
