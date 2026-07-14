DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION
    WHEN OTHERS THEN
        NULL;
END
$$;

-- Tenant row-security policy is intentionally versioned in migration 0008.
-- Several protected tables are themselves created by migrations, so applying
-- the complete policy set here would run before those relations exist.

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
    archived_at TIMESTAMPTZ,
    human_id TEXT,
    tenant_id TEXT,
    tier TEXT NOT NULL DEFAULT 'working',
    access_count BIGINT NOT NULL DEFAULT 0,
    last_accessed TIMESTAMPTZ,
    event_time TIMESTAMPTZ,
    valid_from TIMESTAMPTZ,
    valid_until TIMESTAMPTZ,
    source_agent TEXT,
    visibility TEXT NOT NULL DEFAULT 'public',
    wiki_article TEXT,
    holder TEXT
);

ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS wiki_article TEXT;

ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS tenant_id TEXT;

-- Parity with SQLite schema.sql / dataclass defaults (postgres-parity audit).
-- Forward-migrate these columns on pre-existing Postgres DBs created before
-- the parity fix, BEFORE the 0004 query_cache trigger references valid_from/
-- valid_until. Postgres supports ADD COLUMN IF NOT EXISTS natively.
ALTER TABLE claims ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'working';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS access_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS last_accessed TIMESTAMPTZ;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS event_time TIMESTAMPTZ;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS source_agent TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'public';
-- takes_vs_facts (gbrain multi-holder-belief dimension): nullable holder of the
-- belief. NULL = holder-agnostic (default, byte-identical to pre-holder rows).
ALTER TABLE claims ADD COLUMN IF NOT EXISTS holder TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_claims_identity_visibility_owner'
          AND conrelid = 'claims'::regclass
    ) THEN
        ALTER TABLE claims ADD CONSTRAINT ck_claims_identity_visibility_owner
            CHECK (
                visibility IN ('public', 'private', 'sensitive')
                AND NULLIF(BTRIM(source_agent), '') IS NOT NULL
            );
    END IF;
END
$$;

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
$$;

DROP TRIGGER IF EXISTS trg_claims_supersession_boundary ON claims;
CREATE TRIGGER trg_claims_supersession_boundary
BEFORE INSERT OR UPDATE OF tenant_id, scope, visibility, source_agent,
    supersedes_claim_id, replaced_by_claim_id ON claims
FOR EACH ROW
EXECUTE FUNCTION public.memorymaster_claim_supersession_guard();

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
    created_at TIMESTAMPTZ NOT NULL,
    prev_event_hash TEXT,
    event_hash TEXT,
    hash_algo TEXT,
    tenant_id TEXT,
    tenant_prev_event_hash TEXT,
    tenant_event_hash TEXT,
    tenant_hash_algo TEXT
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

CREATE INDEX IF NOT EXISTS idx_claims_human_id ON claims(human_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_public_human_id_unique
    ON claims(COALESCE(tenant_id, ''), scope, human_id)
    WHERE visibility = 'public' AND human_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_nonpublic_principal_human_id_unique
    ON claims(COALESCE(tenant_id, ''), scope, visibility, source_agent, human_id)
    WHERE visibility <> 'public' AND source_agent IS NOT NULL
      AND human_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_claims_tenant_id ON claims(tenant_id);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_updated_at ON claims(updated_at);
CREATE INDEX IF NOT EXISTS idx_claims_idempotency_key ON claims(idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_public_idempotency_key_unique
    ON claims(COALESCE(tenant_id, ''), scope, idempotency_key)
    WHERE visibility = 'public' AND idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_nonpublic_principal_idempotency_key_unique
    ON claims(COALESCE(tenant_id, ''), scope, visibility, source_agent, idempotency_key)
    WHERE visibility <> 'public' AND source_agent IS NOT NULL
      AND idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_public_confirmed_tuple_unique
    ON claims(COALESCE(tenant_id, ''), subject, predicate, scope)
    WHERE visibility = 'public' AND status = 'confirmed'
      AND subject IS NOT NULL
      AND predicate IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_nonpublic_principal_confirmed_tuple_unique
    ON claims(COALESCE(tenant_id, ''), visibility, source_agent, subject, predicate, scope)
    WHERE visibility <> 'public' AND source_agent IS NOT NULL
      AND status = 'confirmed'
      AND subject IS NOT NULL
      AND predicate IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_claims_tuple ON claims(subject, predicate, scope);
CREATE INDEX IF NOT EXISTS idx_claims_replaced_by ON claims(replaced_by_claim_id);
CREATE INDEX IF NOT EXISTS idx_citations_claim_id ON citations(claim_id);
CREATE INDEX IF NOT EXISTS idx_events_claim_id ON events(claim_id);
CREATE INDEX IF NOT EXISTS idx_events_tenant_id ON events(tenant_id);
CREATE INDEX IF NOT EXISTS idx_events_tenant_hash
    ON events(tenant_id, tenant_event_hash);
CREATE INDEX IF NOT EXISTS idx_events_tenant_head
    ON events(tenant_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_events_tenant_algo_head
    ON events(tenant_id, hash_algo, id DESC);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_type_created_id
    ON events(event_type, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_details_created
    ON events(event_type, details, created_at DESC);

CREATE TABLE IF NOT EXISTS external_sources (
    id BIGSERIAL PRIMARY KEY,
    source_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    config_json JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (source_type, display_name)
);

CREATE TABLE IF NOT EXISTS source_items (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
    source_item_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    chat_id TEXT,
    sender_id TEXT,
    sender_name TEXT,
    occurred_at TIMESTAMPTZ,
    text TEXT,
    payload_json JSONB,
    content_hash TEXT,
    sensitivity TEXT CHECK (sensitivity IS NULL OR sensitivity IN ('none','low','medium','high','redacted')),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (source_id, source_item_id)
);

CREATE TABLE IF NOT EXISTS evidence_items (
    id BIGSERIAL PRIMARY KEY,
    source_item_id BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    text TEXT,
    media_path TEXT,
    provider TEXT,
    confidence DOUBLE PRECISION CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    payload_json JSONB,
    sensitivity TEXT CHECK (sensitivity IS NULL OR sensitivity IN ('none','low','medium','high','redacted')),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS action_proposals (
    id BIGSERIAL PRIMARY KEY,
    proposal_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    source_item_id BIGINT REFERENCES source_items(id) ON DELETE SET NULL,
    evidence_item_id BIGINT REFERENCES evidence_items(id) ON DELETE SET NULL,
    claim_id BIGINT REFERENCES claims(id) ON DELETE SET NULL,
    suggested_due_at TIMESTAMPTZ,
    destination TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'approved', 'rejected', 'exported', 'failed')),
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    payload_json JSONB,
    exported_at TIMESTAMPTZ,
    external_ref TEXT,
    idempotency_key TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_usage (
    id SERIAL PRIMARY KEY,
    tool_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    latency_ms INTEGER,
    tenant_id TEXT,
    result_status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_external_sources_type ON external_sources(source_type);
CREATE INDEX IF NOT EXISTS idx_source_items_source_id ON source_items(source_id);
CREATE INDEX IF NOT EXISTS idx_source_items_chat_id ON source_items(chat_id);
CREATE INDEX IF NOT EXISTS idx_source_items_occurred_at ON source_items(occurred_at);
CREATE INDEX IF NOT EXISTS idx_source_items_content_hash ON source_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_evidence_items_source_item_id ON evidence_items(source_item_id);
CREATE INDEX IF NOT EXISTS idx_evidence_items_type ON evidence_items(evidence_type);
CREATE INDEX IF NOT EXISTS idx_action_proposals_status ON action_proposals(status);
CREATE INDEX IF NOT EXISTS idx_action_proposals_destination ON action_proposals(destination);
CREATE UNIQUE INDEX IF NOT EXISTS idx_action_proposals_idempotency_key
    ON action_proposals(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
-- Forward-migrate sensitivity column on stale Atlas DBs (PR #20 era)
-- BEFORE creating indexes that reference it. Postgres supports
-- ADD COLUMN IF NOT EXISTS natively; SQLite uses an ALTER+catch in
-- _storage_schema._ensure_atlas_source_schema. See PR #27 / claim mm-ce8b.
ALTER TABLE source_items ADD COLUMN IF NOT EXISTS sensitivity TEXT
    CHECK (sensitivity IS NULL OR sensitivity IN ('none','low','medium','high','redacted'));
ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS sensitivity TEXT
    CHECK (sensitivity IS NULL OR sensitivity IN ('none','low','medium','high','redacted'));
CREATE INDEX IF NOT EXISTS idx_source_items_sensitivity ON source_items(sensitivity);
CREATE INDEX IF NOT EXISTS idx_evidence_items_sensitivity ON evidence_items(sensitivity);

CREATE TABLE IF NOT EXISTS media_retry_queue (
    id BIGSERIAL PRIMARY KEY,
    source_item_id BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    media_key TEXT NOT NULL,
    chat_id TEXT,
    media_type TEXT,
    media_path TEXT,
    media_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'retrying', 'expired', 'done', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_http_status INTEGER,
    last_error TEXT,
    next_attempt_time TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (source_item_id, media_key)
);

CREATE INDEX IF NOT EXISTS idx_media_retry_status ON media_retry_queue(status);
CREATE INDEX IF NOT EXISTS idx_media_retry_next_attempt ON media_retry_queue(next_attempt_time);
CREATE INDEX IF NOT EXISTS idx_media_retry_source_item ON media_retry_queue(source_item_id);

CREATE TABLE IF NOT EXISTS claim_links (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    target_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    link_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CHECK (source_id <> target_id),
    CHECK (link_type IN ('relates_to', 'supersedes', 'derived_from', 'contradicts', 'supports'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_links_unique ON claim_links(source_id, target_id, link_type);
CREATE INDEX IF NOT EXISTS idx_claim_links_source ON claim_links(source_id);
CREATE INDEX IF NOT EXISTS idx_claim_links_target ON claim_links(target_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        EXECUTE '
            CREATE TABLE IF NOT EXISTS claim_embeddings (
                claim_id BIGINT PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                content_hash TEXT NOT NULL DEFAULT '''',
                embedding VECTOR(1536) NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
        ';
        EXECUTE '
            CREATE INDEX IF NOT EXISTS idx_embeddings_vector
            ON claim_embeddings USING hnsw (embedding vector_cosine_ops)
        ';
    ELSE
        EXECUTE '
            CREATE TABLE IF NOT EXISTS claim_embeddings (
                claim_id BIGINT PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                content_hash TEXT NOT NULL DEFAULT '''',
                embedding_json TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
        ';
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_embeddings_updated_at ON claim_embeddings(updated_at);

CREATE TABLE IF NOT EXISTS qdrant_sync_state (
    stream_key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    last_claim_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL
);
