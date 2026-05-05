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
    archived_at TIMESTAMPTZ,
    human_id TEXT,
    tenant_id TEXT,
    wiki_article TEXT
);

ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS wiki_article TEXT;

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

CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_human_id ON claims(human_id);
CREATE INDEX IF NOT EXISTS idx_claims_tenant_id ON claims(tenant_id);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_updated_at ON claims(updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_idempotency_key ON claims(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_claims_tuple ON claims(subject, predicate, scope);
CREATE INDEX IF NOT EXISTS idx_claims_replaced_by ON claims(replaced_by_claim_id);
CREATE INDEX IF NOT EXISTS idx_citations_claim_id ON citations(claim_id);
CREATE INDEX IF NOT EXISTS idx_events_claim_id ON events(claim_id);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);

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
                embedding_json TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
        ';
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_embeddings_updated_at ON claim_embeddings(updated_at);
