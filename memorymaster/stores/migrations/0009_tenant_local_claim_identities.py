"""Make claim identity constraints tenant-local on both storage backends."""
from __future__ import annotations

VERSION = 9
DESCRIPTION = "Tenant-local claim identities and confirmed tuples"

_SQLITE_DDL = """
DROP INDEX IF EXISTS idx_claims_idempotency_key;
DROP INDEX IF EXISTS idx_claims_human_id;
DROP INDEX IF EXISTS idx_claims_confirmed_tuple_unique;
CREATE INDEX IF NOT EXISTS idx_claims_idempotency_key ON claims(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_claims_human_id ON claims(human_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_tenant_idempotency_key
    ON claims(COALESCE(tenant_id, ''), idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_tenant_human_id
    ON claims(COALESCE(tenant_id, ''), human_id)
    WHERE human_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_confirmed_tuple_unique
    ON claims(COALESCE(tenant_id, ''), subject, predicate, scope)
    WHERE status = 'confirmed'
      AND subject IS NOT NULL
      AND predicate IS NOT NULL;
DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_insert;
DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_update;
CREATE TRIGGER trg_claims_confirmed_tuple_guard_insert
BEFORE INSERT ON claims
WHEN NEW.status = 'confirmed'
  AND NEW.subject IS NOT NULL
  AND NEW.predicate IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM claims c
    WHERE c.status = 'confirmed'
      AND c.subject = NEW.subject
      AND c.predicate = NEW.predicate
      AND c.scope = NEW.scope
      AND c.tenant_id IS NEW.tenant_id
  )
BEGIN
    SELECT RAISE(ABORT, 'only one confirmed claim is allowed per tenant and (subject,predicate,scope)');
END;
CREATE TRIGGER trg_claims_confirmed_tuple_guard_update
BEFORE UPDATE OF status, subject, predicate, scope, tenant_id ON claims
WHEN NEW.status = 'confirmed'
  AND NEW.subject IS NOT NULL
  AND NEW.predicate IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM claims c
    WHERE c.id <> OLD.id
      AND c.status = 'confirmed'
      AND c.subject = NEW.subject
      AND c.predicate = NEW.predicate
      AND c.scope = NEW.scope
      AND c.tenant_id IS NEW.tenant_id
  )
BEGIN
    SELECT RAISE(ABORT, 'only one confirmed claim is allowed per tenant and (subject,predicate,scope)');
END;
""".strip()

_POSTGRES_INDEX_DDL = """
DROP INDEX IF EXISTS idx_claims_idempotency_key;
DROP INDEX IF EXISTS idx_claims_human_id;
DROP INDEX IF EXISTS idx_claims_confirmed_tuple_unique;
CREATE INDEX IF NOT EXISTS idx_claims_idempotency_key ON claims(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_claims_human_id ON claims(human_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_tenant_idempotency_key
    ON claims(COALESCE(tenant_id, ''), idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_tenant_human_id
    ON claims(COALESCE(tenant_id, ''), human_id)
    WHERE human_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_confirmed_tuple_unique
    ON claims(COALESCE(tenant_id, ''), subject, predicate, scope)
    WHERE status = 'confirmed'
      AND subject IS NOT NULL
      AND predicate IS NOT NULL;
""".strip()

_POSTGRES_TRIGGER_DDL = """
CREATE OR REPLACE FUNCTION memorymaster_claims_confirmed_tuple_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = 'confirmed'
       AND NEW.subject IS NOT NULL
       AND NEW.predicate IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM claims c
           WHERE c.status = 'confirmed'
             AND c.subject = NEW.subject
             AND c.predicate = NEW.predicate
             AND c.scope = NEW.scope
             AND c.tenant_id IS NOT DISTINCT FROM NEW.tenant_id
             AND (TG_OP = 'INSERT' OR c.id <> NEW.id)
       ) THEN
        RAISE EXCEPTION 'only one confirmed claim is allowed per tenant and (subject,predicate,scope)'
            USING ERRCODE = '23505';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard ON claims;
CREATE TRIGGER trg_claims_confirmed_tuple_guard
BEFORE INSERT OR UPDATE OF status, subject, predicate, scope, tenant_id ON claims
FOR EACH ROW
EXECUTE FUNCTION memorymaster_claims_confirmed_tuple_guard();
""".strip()


def apply_sqlite(conn) -> None:
    has_claims = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'claims'"
    ).fetchone()
    if has_claims is None:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(claims)")}
    if "tenant_id" not in columns:
        conn.execute("ALTER TABLE claims ADD COLUMN tenant_id TEXT")
    conn.executescript(_SQLITE_DDL)
    conn.commit()


def apply_postgres(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_POSTGRES_INDEX_DDL)
        cur.execute(_POSTGRES_TRIGGER_DDL)
    conn.commit()
