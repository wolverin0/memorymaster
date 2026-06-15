"""0004_query_cache — correctness-safe result cache for retrieval (gbrain v0.40.3).

Two tables plus write-triggers on ``claims``:

- ``cache_meta`` holds a single monotonic ``corpus_generation`` counter.
- INSERT/DELETE and column-scoped UPDATE triggers on ``claims`` bump that
  counter, so any *retrieval-relevant* claim write advances the generation. The
  UPDATE trigger deliberately EXCLUDES ``access_count``/``last_accessed`` —
  otherwise recording an access on every query would invalidate the cache it
  just served. A cache row is valid only if the generation it was written at
  still equals the current corpus generation.
- ``query_cache`` stores serialized retrieval results keyed by a hash that
  folds in the query, params, AND the retrieval config fingerprint (so a
  weight/mode/floor change also invalidates).

The cache is opt-in (``MEMORYMASTER_QUERY_CACHE=1``); the triggers are always
active so the generation stays accurate, but a single-row integer bump per
claim write is negligible.
"""
from __future__ import annotations

VERSION = 4
DESCRIPTION = "query_cache + cache_meta + claims generation triggers (correctness-safe recall cache)"

_SQLITE_TABLES = """
CREATE TABLE IF NOT EXISTS cache_meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO cache_meta(key, value) VALUES ('corpus_generation', 0);
CREATE TABLE IF NOT EXISTS query_cache (
    cache_key TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    generation INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
""".strip()

# Triggers depend on the claims table; created only when it exists (it always
# does in a real DB — baseline schema precedes migrations — but the migration
# unit tests apply on a bare connection).
_SQLITE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS claims_gen_ai AFTER INSERT ON claims BEGIN
    UPDATE cache_meta SET value = value + 1 WHERE key = 'corpus_generation';
END;
CREATE TRIGGER IF NOT EXISTS claims_gen_au AFTER UPDATE OF
    text, normalized_text, subject, predicate, object_value, scope,
    confidence, status, pinned, tier, volatility, valid_from, valid_until,
    archived_at, updated_at, last_validated_at
ON claims BEGIN
    UPDATE cache_meta SET value = value + 1 WHERE key = 'corpus_generation';
END;
CREATE TRIGGER IF NOT EXISTS claims_gen_ad AFTER DELETE ON claims BEGIN
    UPDATE cache_meta SET value = value + 1 WHERE key = 'corpus_generation';
END;
""".strip()

_POSTGRES_TABLES = """
CREATE TABLE IF NOT EXISTS cache_meta (
    key TEXT PRIMARY KEY,
    value BIGINT NOT NULL
);
INSERT INTO cache_meta(key, value) VALUES ('corpus_generation', 0)
    ON CONFLICT (key) DO NOTHING;
CREATE TABLE IF NOT EXISTS query_cache (
    cache_key TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    generation BIGINT NOT NULL,
    created_at TEXT NOT NULL
);
""".strip()

_POSTGRES_TRIGGERS = """
CREATE OR REPLACE FUNCTION mm_bump_corpus_generation() RETURNS trigger AS $$
BEGIN
    UPDATE cache_meta SET value = value + 1 WHERE key = 'corpus_generation';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS claims_gen_ins_del ON claims;
CREATE TRIGGER claims_gen_ins_del
    AFTER INSERT OR DELETE ON claims
    FOR EACH STATEMENT EXECUTE FUNCTION mm_bump_corpus_generation();
DROP TRIGGER IF EXISTS claims_gen_upd ON claims;
CREATE TRIGGER claims_gen_upd
    AFTER UPDATE OF text, normalized_text, subject, predicate, object_value,
        scope, confidence, status, pinned, tier, volatility, valid_from,
        valid_until, archived_at, updated_at, last_validated_at ON claims
    FOR EACH STATEMENT EXECUTE FUNCTION mm_bump_corpus_generation();
""".strip()


def apply_sqlite(conn) -> None:
    conn.executescript(_SQLITE_TABLES)
    has_claims = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'"
    ).fetchone()
    if has_claims:
        conn.executescript(_SQLITE_TRIGGERS)
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def apply_postgres(conn) -> None:
    cur = conn.cursor()
    cur.execute(_POSTGRES_TABLES)
    cur.execute("SELECT to_regclass('claims')")
    row = cur.fetchone()
    if row and row[0] is not None:
        cur.execute(_POSTGRES_TRIGGERS)
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()
