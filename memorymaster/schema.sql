PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    supersedes_claim_id INTEGER,
    replaced_by_claim_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_validated_at TEXT,
    archived_at TEXT,
    human_id TEXT,
    tier TEXT NOT NULL DEFAULT 'working',
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed TEXT,
    event_time TEXT,
    valid_from TEXT,
    valid_until TEXT,
    wiki_article TEXT,
    FOREIGN KEY (supersedes_claim_id) REFERENCES claims(id) ON DELETE SET NULL,
    FOREIGN KEY (replaced_by_claim_id) REFERENCES claims(id) ON DELETE SET NULL
);

CREATE TRIGGER IF NOT EXISTS trg_claims_confirmed_tuple_guard_insert
BEFORE INSERT ON claims
WHEN NEW.status = 'confirmed'
  AND NEW.subject IS NOT NULL
  AND NEW.predicate IS NOT NULL
  AND EXISTS (
    SELECT 1
    FROM claims c
    WHERE c.status = 'confirmed'
      AND c.subject = NEW.subject
      AND c.predicate = NEW.predicate
      AND c.scope = NEW.scope
  )
BEGIN
    SELECT RAISE(ABORT, 'only one confirmed claim is allowed per (subject,predicate,scope)');
END;

CREATE TRIGGER IF NOT EXISTS trg_claims_confirmed_tuple_guard_update
BEFORE UPDATE OF status, subject, predicate, scope ON claims
WHEN NEW.status = 'confirmed'
  AND NEW.subject IS NOT NULL
  AND NEW.predicate IS NOT NULL
  AND EXISTS (
    SELECT 1
    FROM claims c
    WHERE c.id <> OLD.id
      AND c.status = 'confirmed'
      AND c.subject = NEW.subject
      AND c.predicate = NEW.predicate
      AND c.scope = NEW.scope
  )
BEGIN
    SELECT RAISE(ABORT, 'only one confirmed claim is allowed per (subject,predicate,scope)');
END;

CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    locator TEXT,
    excerpt TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    details TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS trg_events_append_only_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only; UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_events_append_only_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only; DELETE is not allowed');
END;

CREATE TABLE IF NOT EXISTS claim_embeddings (
    claim_id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS external_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    config_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_type, display_name)
);

CREATE TABLE IF NOT EXISTS source_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    source_item_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    chat_id TEXT,
    sender_id TEXT,
    sender_name TEXT,
    occurred_at TEXT,
    text TEXT,
    payload_json TEXT,
    content_hash TEXT,
    sensitivity TEXT CHECK (sensitivity IS NULL OR sensitivity IN ('none','low','medium','high','redacted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES external_sources(id) ON DELETE CASCADE,
    UNIQUE (source_id, source_item_id)
);

CREATE TABLE IF NOT EXISTS evidence_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_item_id INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,
    text TEXT,
    media_path TEXT,
    provider TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    payload_json TEXT,
    sensitivity TEXT CHECK (sensitivity IS NULL OR sensitivity IN ('none','low','medium','high','redacted')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_item_id) REFERENCES source_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS action_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    source_item_id INTEGER,
    evidence_item_id INTEGER,
    claim_id INTEGER,
    suggested_due_at TEXT,
    destination TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'approved', 'rejected', 'exported', 'failed')),
    confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    payload_json TEXT,
    exported_at TEXT,
    external_ref TEXT,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_item_id) REFERENCES source_items(id) ON DELETE SET NULL,
    FOREIGN KEY (evidence_item_id) REFERENCES evidence_items(id) ON DELETE SET NULL,
    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS mcp_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    latency_ms INTEGER,
    tenant_id TEXT,
    result_status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_updated_at ON claims(updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_idempotency_key ON claims(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_claims_tuple ON claims(subject, predicate, scope);
CREATE INDEX IF NOT EXISTS idx_claims_replaced_by ON claims(replaced_by_claim_id);
CREATE INDEX IF NOT EXISTS idx_citations_claim_id ON citations(claim_id);
CREATE INDEX IF NOT EXISTS idx_events_claim_id ON events(claim_id);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_embeddings_updated_at ON claim_embeddings(updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_human_id ON claims(human_id);
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
-- sensitivity indexes are created by _storage_schema._ensure_atlas_source_schema
-- AFTER its idempotent ALTER TABLE migration. Keeping them here would fail on
-- stale Atlas DBs (PR #20 era) that don't yet have the sensitivity column,
-- triggering the storage.py lenient-fallback path. See PR #27 / claim mm-ce8b.

CREATE TABLE IF NOT EXISTS media_retry_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_item_id INTEGER NOT NULL,
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
    next_attempt_time TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_item_id) REFERENCES source_items(id) ON DELETE CASCADE,
    UNIQUE (source_item_id, media_key)
);

CREATE INDEX IF NOT EXISTS idx_media_retry_status ON media_retry_queue(status);
CREATE INDEX IF NOT EXISTS idx_media_retry_next_attempt ON media_retry_queue(next_attempt_time);
CREATE INDEX IF NOT EXISTS idx_media_retry_source_item ON media_retry_queue(source_item_id);
