"""Partition claim identities by scope, visibility, and principal."""
from __future__ import annotations


VERSION = 12
DESCRIPTION = "Scope- and principal-local claim identities"

POSTGRES_IDENTITY_PREFLIGHT_SQL = """
SELECT visibility, COUNT(*) AS ownerless_claims
FROM claims
WHERE NULLIF(BTRIM(source_agent), '') IS NULL
GROUP BY visibility
ORDER BY visibility
""".strip()

POSTGRES_SUPERSESSION_PREFLIGHT_SQL = """
SELECT COUNT(*) AS invalid_supersession_edges
FROM claims AS claim
LEFT JOIN claims AS superseded
  ON superseded.id = claim.supersedes_claim_id
LEFT JOIN claims AS replacement
  ON replacement.id = claim.replaced_by_claim_id
WHERE (
    claim.supersedes_claim_id IS NOT NULL
    AND (
        claim.supersedes_claim_id = claim.id
        OR superseded.id IS NULL
        OR superseded.tenant_id IS DISTINCT FROM claim.tenant_id
        OR superseded.scope IS DISTINCT FROM claim.scope
        OR superseded.visibility IS DISTINCT FROM claim.visibility
        OR superseded.source_agent IS DISTINCT FROM claim.source_agent
        OR superseded.replaced_by_claim_id IS DISTINCT FROM claim.id
    )
) OR (
    claim.replaced_by_claim_id IS NOT NULL
    AND (
        claim.replaced_by_claim_id = claim.id
        OR replacement.id IS NULL
        OR replacement.tenant_id IS DISTINCT FROM claim.tenant_id
        OR replacement.scope IS DISTINCT FROM claim.scope
        OR replacement.visibility IS DISTINCT FROM claim.visibility
        OR replacement.source_agent IS DISTINCT FROM claim.source_agent
        OR replacement.supersedes_claim_id IS DISTINCT FROM claim.id
    )
)
""".strip()

_SUPERSESSION_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION public.memorymaster_claim_supersession_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    reference_id BIGINT;
BEGIN
    FOREACH reference_id IN ARRAY ARRAY[
        NEW.supersedes_claim_id,
        NEW.replaced_by_claim_id
    ] LOOP
        IF reference_id IS NOT NULL AND (
            reference_id = NEW.id
            OR NOT EXISTS (
                SELECT 1
                FROM public.claims AS referenced
                WHERE referenced.id = reference_id
                  AND referenced.tenant_id IS NOT DISTINCT FROM NEW.tenant_id
                  AND referenced.scope = NEW.scope
                  AND referenced.visibility IS NOT DISTINCT FROM NEW.visibility
                  AND referenced.source_agent IS NOT DISTINCT FROM NEW.source_agent
            )
        ) THEN
            RAISE EXCEPTION 'supersession reference is outside the authorized boundary'
                USING ERRCODE = '42501';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$
""".strip()

_SUPERSESSION_GUARD_TRIGGER = """
DROP TRIGGER IF EXISTS trg_claims_supersession_boundary ON claims;
CREATE TRIGGER trg_claims_supersession_boundary
BEFORE INSERT OR UPDATE OF tenant_id, scope, visibility, source_agent,
    supersedes_claim_id, replaced_by_claim_id ON claims
FOR EACH ROW
EXECUTE FUNCTION public.memorymaster_claim_supersession_guard();
""".strip()


_DROP_LEGACY_SQLITE = """
DROP INDEX IF EXISTS idx_claims_tenant_idempotency_key;
DROP INDEX IF EXISTS idx_claims_tenant_human_id;
DROP INDEX IF EXISTS idx_claims_confirmed_tuple_unique;
DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_insert;
DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_update;
"""

_IDENTITY_INDEXES_SQLITE = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_public_idempotency_key_unique
    ON claims(COALESCE(tenant_id, ''), scope, idempotency_key)
    WHERE visibility = 'public' AND idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_nonpublic_principal_idempotency_key_unique
    ON claims(COALESCE(tenant_id, ''), scope, visibility, source_agent, idempotency_key)
    WHERE visibility <> 'public' AND source_agent IS NOT NULL
      AND idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_public_human_id_unique
    ON claims(COALESCE(tenant_id, ''), scope, human_id)
    WHERE visibility = 'public' AND human_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_nonpublic_principal_human_id_unique
    ON claims(COALESCE(tenant_id, ''), scope, visibility, source_agent, human_id)
    WHERE visibility <> 'public' AND source_agent IS NOT NULL
      AND human_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_public_confirmed_tuple_unique
    ON claims(COALESCE(tenant_id, ''), subject, predicate, scope)
    WHERE visibility = 'public' AND status = 'confirmed'
      AND subject IS NOT NULL AND predicate IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_nonpublic_principal_confirmed_tuple_unique
    ON claims(COALESCE(tenant_id, ''), visibility, source_agent, subject, predicate, scope)
    WHERE visibility <> 'public' AND source_agent IS NOT NULL
      AND status = 'confirmed' AND subject IS NOT NULL AND predicate IS NOT NULL;
"""

_IDENTITY_GUARDS_SQLITE = """
CREATE TRIGGER IF NOT EXISTS trg_claims_identity_guard_insert
BEFORE INSERT ON claims
WHEN NEW.visibility NOT IN ('public', 'private', 'sensitive')
  OR (NEW.visibility <> 'public' AND NULLIF(TRIM(NEW.source_agent), '') IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'invalid claim visibility or missing non-public source_agent');
END;
CREATE TRIGGER IF NOT EXISTS trg_claims_identity_guard_update
BEFORE UPDATE ON claims
WHEN NEW.visibility NOT IN ('public', 'private', 'sensitive')
  OR (NEW.visibility <> 'public' AND NULLIF(TRIM(NEW.source_agent), '') IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'invalid claim visibility or missing non-public source_agent');
END;
"""

_POSTGRES_DDL = """
DROP INDEX IF EXISTS idx_claims_tenant_idempotency_key;
DROP INDEX IF EXISTS idx_claims_tenant_human_id;
DROP INDEX IF EXISTS idx_claims_confirmed_tuple_unique;
DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard ON claims;
DROP FUNCTION IF EXISTS memorymaster_claims_confirmed_tuple_guard();
ALTER TABLE claims DROP CONSTRAINT IF EXISTS ck_claims_identity_visibility_owner;
ALTER TABLE claims ADD CONSTRAINT ck_claims_identity_visibility_owner
    CHECK (
        visibility IN ('public', 'private', 'sensitive')
        AND NULLIF(BTRIM(source_agent), '') IS NOT NULL
    ) NOT VALID;
ALTER TABLE claims VALIDATE CONSTRAINT ck_claims_identity_visibility_owner;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_public_idempotency_key_unique
    ON claims(COALESCE(tenant_id, ''), scope, idempotency_key)
    WHERE visibility = 'public' AND idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_nonpublic_principal_idempotency_key_unique
    ON claims(COALESCE(tenant_id, ''), scope, visibility, source_agent, idempotency_key)
    WHERE visibility <> 'public' AND source_agent IS NOT NULL
      AND idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_public_human_id_unique
    ON claims(COALESCE(tenant_id, ''), scope, human_id)
    WHERE visibility = 'public' AND human_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_nonpublic_principal_human_id_unique
    ON claims(COALESCE(tenant_id, ''), scope, visibility, source_agent, human_id)
    WHERE visibility <> 'public' AND source_agent IS NOT NULL
      AND human_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_public_confirmed_tuple_unique
    ON claims(COALESCE(tenant_id, ''), subject, predicate, scope)
    WHERE visibility = 'public' AND status = 'confirmed'
      AND subject IS NOT NULL AND predicate IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_nonpublic_principal_confirmed_tuple_unique
    ON claims(COALESCE(tenant_id, ''), visibility, source_agent, subject, predicate, scope)
    WHERE visibility <> 'public' AND source_agent IS NOT NULL
      AND status = 'confirmed' AND subject IS NOT NULL AND predicate IS NOT NULL;
"""


def apply_sqlite(conn) -> None:
    has_claims = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'claims'"
    ).fetchone()
    if has_claims is None:
        return
    conn.executescript(
        _DROP_LEGACY_SQLITE + _IDENTITY_INDEXES_SQLITE + _IDENTITY_GUARDS_SQLITE
    )
    conn.commit()


def apply_postgres(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(POSTGRES_SUPERSESSION_PREFLIGHT_SQL)
        row = cur.fetchone()
        if isinstance(row, dict):
            invalid_edges = int(row.get("invalid_supersession_edges") or 0)
        elif row is not None:
            invalid_edges = int(row[0])
        else:
            raise RuntimeError("Postgres supersession preflight returned no result.")
        if invalid_edges:
            raise RuntimeError(
                f"Postgres migration refused {invalid_edges} invalid supersession "
                "edge(s); inventory and repair require explicit approval."
            )
        for statement in _POSTGRES_DDL.split(";"):
            if statement.strip():
                cur.execute(statement)
        cur.execute(_SUPERSESSION_GUARD_FUNCTION)
        for statement in _SUPERSESSION_GUARD_TRIGGER.split(";"):
            if statement.strip():
                cur.execute(statement)
    conn.commit()
