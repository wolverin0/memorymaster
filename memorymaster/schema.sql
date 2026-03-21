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
