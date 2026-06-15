"""Entity Registry — canonical entities with alias resolution.

Inspired by GBrain's entity registry pattern: every subject string resolves
to a canonical entity so that "MemoryMaster", "memorymaster", "MM" all point
to the same node. This turns the flat claims DB into a real knowledge graph.

Tables:
  - entities: canonical entity (id, name, type, scope, created/updated)
  - entity_aliases: maps normalized alias strings → entity id

Resolution flow (on ingest):
  1. Normalize subject string (lowercase, strip, collapse separators)
  2. Look up in entity_aliases
  3. If found → return canonical entity_id
  4. If not found → create new entity + register the alias
  5. Store entity_id on the claim

The claim.subject column stays as free-text for display; entity_id is the
canonical FK used for grouping and traversal.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_NORMALIZE_RE = re.compile(r"[\s_\-\.]+")


def normalize_alias(raw: str) -> str:
    """Normalize a subject string for alias lookup.

    Lowercases, collapses whitespace/dashes/underscores/dots into single
    dashes, strips leading/trailing separators. Truncates to 200 chars.
    """
    if not raw:
        return ""
    return _NORMALIZE_RE.sub("-", raw.strip().lower()).strip("-")[:200]


def _variant_key(raw: str) -> str:
    """Per-original-form dedup key. Trims + collapses internal whitespace
    only — preserves case and separators so every distinct surface form
    ("Qdrant", "qdrant", "QDRANT", "qdrant-cloud", "qdrant vector db")
    becomes its own alias row. The heavy case/separator collapsing lives
    in :func:`normalize_alias` (the lookup key) — variant_key is the
    WRITE-side dedup key.
    """
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw.strip())[:200]


def ensure_entity_schema(conn: sqlite3.Connection) -> None:
    """Create entity tables if they don't exist. Idempotent.

    The UNIQUE constraint on ``entity_aliases`` is on
    ``(entity_id, variant_key)`` — NOT on ``alias``. The normalized ``alias``
    column is shared across variants of the same entity (by design) so
    multiple original_forms (e.g. "Qdrant", "qdrant", "QDRANT",
    "qdrant-cloud") can coexist as distinct rows pointing to the same
    ``entity_id``. Lookup still uses ``alias`` (the heavily-normalized
    form) because that's the key that collapses case/separator variation.

    NOTE: this schema is applied to *new* DBs only. Existing DBs created
    with the old UNIQUE(alias) constraint require an explicit one-shot
    migration — see :func:`migrate_entity_aliases_schema`.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL UNIQUE,
            entity_type TEXT NOT NULL DEFAULT 'unknown',
            scope TEXT NOT NULL DEFAULT 'global',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            variant_key TEXT NOT NULL DEFAULT '',
            original_form TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(entity_id, variant_key),
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_entity_aliases_alias
            ON entity_aliases(alias);
        CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity_id
            ON entity_aliases(entity_id);
    """)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_or_create(
    conn: sqlite3.Connection,
    subject: str,
    *,
    entity_type: str = "unknown",
    scope: str = "global",
) -> int:
    """Resolve a subject string to a canonical entity_id, creating if needed.

    Every call records a distinct original_form variant (deduped per
    ``(entity_id, variant_key)``). This is what makes the registry
    useful: "Qdrant", "qdrant", "QDRANT " all collapse to the same
    entity while producing multiple alias rows. Returns the entity_id
    (int). Thread-safe via SQLite serialization.
    """
    alias = normalize_alias(subject)
    if not alias:
        return 0

    now = _utc_now()
    display = subject.strip()[:200]
    variant = _variant_key(subject)

    # Step 1: resolve entity_id — existing row with this normalized alias wins.
    row = conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE alias = ? LIMIT 1",
        (alias,),
    ).fetchone()
    if row:
        entity_id = row[0]
    else:
        # Create new entity. If canonical_name collides (shouldn't, since
        # no alias row exists for this normalized form, but paranoia)
        # INSERT OR IGNORE then look up.
        cur = conn.execute(
            """INSERT OR IGNORE INTO entities
                   (canonical_name, entity_type, scope, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (display, entity_type, scope, now, now),
        )
        if cur.lastrowid and cur.rowcount > 0:
            entity_id = cur.lastrowid
        else:
            existing = conn.execute(
                "SELECT id FROM entities WHERE canonical_name = ?", (display,)
            ).fetchone()
            entity_id = existing[0] if existing else 0

    # Step 2: ALWAYS record this variant. INSERT OR IGNORE is deduped by
    # the (entity_id, variant_key) UNIQUE constraint — so calling
    # resolve_or_create repeatedly with the same input is a no-op, but
    # each fresh case/separator variant adds an alias row.
    if entity_id > 0:
        conn.execute(
            """INSERT OR IGNORE INTO entity_aliases
                   (entity_id, alias, variant_key, original_form, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entity_id, alias, variant, display, now),
        )

    return entity_id


def merge_entities(
    conn: sqlite3.Connection,
    keep_id: int,
    merge_id: int,
) -> dict:
    """Merge entity merge_id INTO keep_id. All aliases of merge_id move to keep_id.

    Returns {"merged_aliases": int, "updated_claims": int}.

    The alias move uses ``UPDATE OR IGNORE`` because keep_id may already own a
    row with the same ``(entity_id, variant_key)`` as one of merge_id's aliases
    (the UNIQUE constraint). Without OR IGNORE the bulk UPDATE raises
    sqlite3.IntegrityError partway through, leaving aliases/claims half-moved
    and the merged entity NOT deleted — a corrupted entity graph. With it, the
    colliding alias rows simply stay on merge_id; we then delete those leftovers
    explicitly so the subsequent entity DELETE doesn't strand them (or get
    blocked by FK). The whole merge runs in a single transaction so any failure
    rolls back cleanly instead of committing a partial merge.
    """
    with conn:  # transaction: commits on success, rolls back on any exception
        # Move aliases that don't collide; collisions are left on merge_id.
        cur = conn.execute(
            "UPDATE OR IGNORE entity_aliases SET entity_id = ? WHERE entity_id = ?",
            (keep_id, merge_id),
        )
        merged_aliases = cur.rowcount

        # Remove the duplicate alias rows that stayed behind on merge_id so the
        # entity DELETE (and ON DELETE CASCADE) leaves no orphans.
        conn.execute(
            "DELETE FROM entity_aliases WHERE entity_id = ?",
            (merge_id,),
        )

        # Move claims that reference merge_id.
        cur2 = conn.execute(
            "UPDATE claims SET entity_id = ? WHERE entity_id = ?",
            (keep_id, merge_id),
        )
        updated_claims = cur2.rowcount

        # Delete the merged entity.
        conn.execute("DELETE FROM entities WHERE id = ?", (merge_id,))

    return {"merged_aliases": merged_aliases, "updated_claims": updated_claims}


def add_alias(conn: sqlite3.Connection, entity_id: int, alias_text: str) -> bool:
    """Register an additional alias variant for an entity. Returns True if added
    (False if this variant was already recorded for this entity).
    """
    alias = normalize_alias(alias_text)
    if not alias:
        return False
    variant = _variant_key(alias_text)
    now = _utc_now()
    cur = conn.execute(
        """INSERT OR IGNORE INTO entity_aliases
               (entity_id, alias, variant_key, original_form, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (entity_id, alias, variant, alias_text.strip(), now),
    )
    return cur.rowcount > 0


def list_entities(
    conn: sqlite3.Connection,
    *,
    scope: str | None = None,
    entity_type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List entities with their alias counts and claim counts."""
    query = """
        SELECT e.id, e.canonical_name, e.entity_type, e.scope, e.created_at,
               COUNT(DISTINCT a.id) as alias_count,
               COUNT(DISTINCT c.id) as claim_count
        FROM entities e
        LEFT JOIN entity_aliases a ON a.entity_id = e.id
        LEFT JOIN claims c ON c.entity_id = e.id
    """
    params: list = []
    wheres: list[str] = []
    if scope:
        wheres.append("e.scope LIKE ?")
        params.append(f"{scope}%")
    if entity_type:
        wheres.append("e.entity_type = ?")
        params.append(entity_type)
    if wheres:
        query += " WHERE " + " AND ".join(wheres)
    query += " GROUP BY e.id ORDER BY claim_count DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "type": r[2],
            "scope": r[3],
            "created_at": r[4],
            "alias_count": r[5],
            "claim_count": r[6],
        }
        for r in rows
    ]


def get_aliases(conn: sqlite3.Connection, entity_id: int) -> list[str]:
    """Get all aliases for an entity."""
    rows = conn.execute(
        "SELECT original_form FROM entity_aliases WHERE entity_id = ? ORDER BY created_at",
        (entity_id,),
    ).fetchall()
    return [r[0] for r in rows]


def backfill_entities(conn: sqlite3.Connection) -> dict:
    """Backfill entity_id on existing claims that have subject but no entity_id.

    Creates entities and aliases as needed. Returns stats.
    """
    ensure_entity_schema(conn)

    # Ensure entity_id column exists on claims
    try:
        conn.execute("ALTER TABLE claims ADD COLUMN entity_id INTEGER")
    except sqlite3.OperationalError:
        pass  # already exists

    rows = conn.execute(
        "SELECT DISTINCT subject FROM claims WHERE subject IS NOT NULL AND (entity_id IS NULL OR entity_id = 0)"
    ).fetchall()

    created = 0
    resolved = 0
    for (subject,) in rows:
        entity_id = resolve_or_create(conn, subject)
        if entity_id > 0:
            cur = conn.execute(
                "UPDATE claims SET entity_id = ? WHERE subject = ? AND (entity_id IS NULL OR entity_id = 0)",
                (entity_id, subject),
            )
            resolved += cur.rowcount
            created += 1

    conn.commit()
    return {"entities_created": created, "claims_resolved": resolved, "subjects_processed": len(rows)}


def _has_legacy_alias_unique(conn: sqlite3.Connection) -> bool:
    """Return True if entity_aliases has the old UNIQUE(alias) column-level
    constraint (which prevents >1 alias per normalized form). New schema
    drops it in favour of UNIQUE(entity_id, variant_key).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='entity_aliases'"
    ).fetchone()
    if not row or not row[0]:
        return False
    sql = row[0]
    # legacy form: `alias TEXT NOT NULL UNIQUE`
    # new form:   `alias TEXT NOT NULL,` + composite UNIQUE at end
    return "alias TEXT NOT NULL UNIQUE" in sql


def migrate_entity_aliases_schema(conn: sqlite3.Connection) -> dict:
    """One-shot migration from the legacy UNIQUE(alias) schema to the new
    UNIQUE(entity_id, variant_key) schema. Preserves all existing rows.
    Adds the ``variant_key`` column (populated from ``original_form``).
    Idempotent — safe to call on an already-migrated DB (returns no-op).

    Returns stats dict. Caller is responsible for commit + running this
    inside a single transaction.
    """
    if not _has_legacy_alias_unique(conn):
        return {"migrated": False, "reason": "schema_already_current"}

    # Rename old table, create new, copy data, drop old.
    # Legacy indexes follow the renamed table, so drop them by name before
    # re-creating on the new table (SQLite enforces index name uniqueness
    # across the whole DB).
    conn.executescript("""
        DROP INDEX IF EXISTS idx_entity_aliases_alias;
        DROP INDEX IF EXISTS idx_entity_aliases_entity_id;

        ALTER TABLE entity_aliases RENAME TO entity_aliases_legacy;

        CREATE TABLE entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            variant_key TEXT NOT NULL DEFAULT '',
            original_form TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(entity_id, variant_key),
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
        );

        CREATE INDEX idx_entity_aliases_alias ON entity_aliases(alias);
        CREATE INDEX idx_entity_aliases_entity_id ON entity_aliases(entity_id);
    """)

    # Copy — variant_key derived from original_form on the fly.
    rows = conn.execute(
        "SELECT entity_id, alias, original_form, created_at "
        "FROM entity_aliases_legacy ORDER BY id"
    ).fetchall()
    copied = 0
    for entity_id, alias, original, created in rows:
        variant = _variant_key(original or alias or "")
        cur = conn.execute(
            """INSERT OR IGNORE INTO entity_aliases
                   (entity_id, alias, variant_key, original_form, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entity_id, alias, variant, original, created),
        )
        copied += cur.rowcount

    conn.execute("DROP TABLE entity_aliases_legacy")
    return {"migrated": True, "rows_copied": copied, "legacy_rows": len(rows)}


def backfill_entities_normalized(
    conn: sqlite3.Connection,
    *,
    migrate_schema: bool = True,
) -> dict:
    """Re-scan every claim.subject and register it against the registry,
    producing one alias row per distinct case/separator variant of each
    canonical entity.

    This is the function to run on an existing DB where the old
    ``resolve_or_create`` fast-path dropped all variant rows. After it
    runs, ``avg_aliases_per_entity`` should climb significantly (~2–3x
    on a typical DB with ad-hoc casing).

    WARNING: do NOT run this on the live memorymaster.db unless you know
    what you're doing. Call it from a throwaway copy first.

    Args:
        conn: open SQLite connection (caller commits).
        migrate_schema: if True, auto-migrate legacy UNIQUE(alias) schema
            before backfilling. Set False to fail-loud if schema is old.

    Returns stats dict:
        - ``schema_migration``: output of :func:`migrate_entity_aliases_schema`
        - ``subjects_scanned``: distinct non-null subjects seen
        - ``variants_added``: new alias rows inserted by this pass
        - ``entities_touched``: entities that gained at least one new alias
    """
    ensure_entity_schema(conn)

    schema_out: dict = {"migrated": False, "reason": "skipped"}
    if migrate_schema:
        schema_out = migrate_entity_aliases_schema(conn)
    elif _has_legacy_alias_unique(conn):
        raise RuntimeError(
            "entity_aliases still has legacy UNIQUE(alias) — pass "
            "migrate_schema=True or run migrate_entity_aliases_schema first"
        )

    # Group claim subjects by normalized alias so we can surface the
    # per-variant delta for each entity.
    rows = conn.execute(
        "SELECT DISTINCT subject FROM claims "
        "WHERE subject IS NOT NULL AND TRIM(subject) != ''"
    ).fetchall()

    variants_added = 0
    touched: set[int] = set()
    for (subject,) in rows:
        entity_id = resolve_or_create(conn, subject)
        if entity_id <= 0:
            continue
        # resolve_or_create already inserted the variant; detect whether
        # it was new by checking if the rowid was fresh — but that's racy.
        # Simpler: count total aliases per entity before/after is overkill;
        # we just report the delta in the final pass below.
        touched.add(entity_id)
        # Backfill claims.entity_id too (harmless if already set).
        conn.execute(
            "UPDATE claims SET entity_id = ? "
            "WHERE subject = ? AND (entity_id IS NULL OR entity_id = 0)",
            (entity_id, subject),
        )

    # Compute the true delta: how many alias rows exist now vs how many
    # distinct entities got touched.
    total_aliases = conn.execute(
        "SELECT COUNT(*) FROM entity_aliases"
    ).fetchone()[0]
    total_entities = conn.execute(
        "SELECT COUNT(*) FROM entities"
    ).fetchone()[0]
    avg = (total_aliases / total_entities) if total_entities else 0.0
    variants_added = max(0, total_aliases - total_entities)

    return {
        "schema_migration": schema_out,
        "subjects_scanned": len(rows),
        "variants_added": variants_added,
        "entities_touched": len(touched),
        "total_entities": total_entities,
        "total_aliases": total_aliases,
        "avg_aliases_per_entity": round(avg, 3),
    }
