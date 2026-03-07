DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION
    WHEN OTHERS THEN
        NULL;
END
$$;

CREATE TABLE IF NOT EXISTS claims (
    id BIGSERIAL PRIMARY KEY,
    text TEXT NOT NULL,
    idempotency_key TEXT,
    normalized_text TEXT,
    claim_type TEXT,
    subject TEXT,
    predicate TEXT,
    object_value TEXT,
    scope TEXT NOT NULL DEFAULT 'project',
    volatility TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL CHECK (
        status IN ('candidate', 'confirmed', 'stale', 'superseded', 'conflicted', 'archived')
    ),
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    pinned BOOLEAN NOT NULL DEFAULT FALSE,
    supersedes_claim_id BIGINT REFERENCES claims(id) ON DELETE SET NULL,
    replaced_by_claim_id BIGINT REFERENCES claims(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    last_validated_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ
);

ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE OR REPLACE FUNCTION memorymaster_claims_confirmed_tuple_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = 'confirmed'
       AND NEW.subject IS NOT NULL
       AND NEW.predicate IS NOT NULL
       AND EXISTS (
           SELECT 1
           FROM claims c
           WHERE c.status = 'confirmed'
             AND c.subject = NEW.subject
             AND c.predicate = NEW.predicate
             AND c.scope = NEW.scope
             AND (TG_OP = 'INSERT' OR c.id <> NEW.id)
       ) THEN
        RAISE EXCEPTION 'only one confirmed claim is allowed per (subject,predicate,scope)'
            USING ERRCODE = '23505';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'trg_claims_confirmed_tuple_guard'
          AND tgrelid = 'claims'::regclass
    ) THEN
        CREATE TRIGGER trg_claims_confirmed_tuple_guard
        BEFORE INSERT OR UPDATE OF status, subject, predicate, scope ON claims
        FOR EACH ROW
        EXECUTE FUNCTION memorymaster_claims_confirmed_tuple_guard();
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS citations (
    id BIGSERIAL PRIMARY KEY,
    claim_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    locator TEXT,
    excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    claim_id BIGINT REFERENCES claims(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    details TEXT,
    payload_json JSONB,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION memorymaster_events_append_only_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'events table is append-only; % is not allowed', TG_OP;
END;
$$;

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

CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_updated_at ON claims(updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_idempotency_key ON claims(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_claims_tuple ON claims(subject, predicate, scope);
CREATE INDEX IF NOT EXISTS idx_claims_replaced_by ON claims(replaced_by_claim_id);
CREATE INDEX IF NOT EXISTS idx_citations_claim_id ON citations(claim_id);
CREATE INDEX IF NOT EXISTS idx_events_claim_id ON events(claim_id);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        EXECUTE '
            CREATE TABLE IF NOT EXISTS claim_embeddings (
                claim_id BIGINT PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                embedding VECTOR(1536) NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
        ';
        EXECUTE '
            CREATE INDEX IF NOT EXISTS idx_embeddings_vector
            ON claim_embeddings USING hnsw (embedding vector_cosine_ops)
        ';
    END IF;
END
$$;
