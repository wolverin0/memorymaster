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


def ensure_entity_schema(conn: sqlite3.Connection) -> None:
    """Create entity tables if they don't exist. Idempotent."""
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
            alias TEXT NOT NULL UNIQUE,
            original_form TEXT NOT NULL,
            created_at TEXT NOT NULL,
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

    Returns the entity_id (int). Thread-safe via SQLite serialization.
    """
    alias = normalize_alias(subject)
    if not alias:
        return 0

    # Fast path: alias already registered
    row = conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE alias = ?", (alias,)
    ).fetchone()
    if row:
        return row[0]

    # Slow path: create entity + register alias
    now = _utc_now()
    display_name = subject.strip()[:200]

    cur = conn.execute(
        """INSERT OR IGNORE INTO entities (canonical_name, entity_type, scope, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (display_name, entity_type, scope, now, now),
    )
    if cur.lastrowid and cur.lastrowid > 0:
        entity_id = cur.lastrowid
    else:
        # INSERT OR IGNORE hit a duplicate canonical_name — fetch existing
        existing = conn.execute(
            "SELECT id FROM entities WHERE canonical_name = ?", (display_name,)
        ).fetchone()
        entity_id = existing[0] if existing else 0

    if entity_id > 0:
        conn.execute(
            """INSERT OR IGNORE INTO entity_aliases (entity_id, alias, original_form, created_at)
               VALUES (?, ?, ?, ?)""",
            (entity_id, alias, subject.strip(), now),
        )

    return entity_id


def merge_entities(
    conn: sqlite3.Connection,
    keep_id: int,
    merge_id: int,
) -> dict:
    """Merge entity merge_id INTO keep_id. All aliases of merge_id move to keep_id.

    Returns {"merged_aliases": int, "updated_claims": int}.
    """
    # Move aliases
    cur = conn.execute(
        "UPDATE entity_aliases SET entity_id = ? WHERE entity_id = ?",
        (keep_id, merge_id),
    )
    merged_aliases = cur.rowcount

    # Move claims that reference merge_id
    cur2 = conn.execute(
        "UPDATE claims SET entity_id = ? WHERE entity_id = ?",
        (keep_id, merge_id),
    )
    updated_claims = cur2.rowcount

    # Delete the merged entity
    conn.execute("DELETE FROM entities WHERE id = ?", (merge_id,))

    return {"merged_aliases": merged_aliases, "updated_claims": updated_claims}


def add_alias(conn: sqlite3.Connection, entity_id: int, alias_text: str) -> bool:
    """Register an additional alias for an entity. Returns True if added."""
    alias = normalize_alias(alias_text)
    if not alias:
        return False
    now = _utc_now()
    try:
        conn.execute(
            """INSERT INTO entity_aliases (entity_id, alias, original_form, created_at)
               VALUES (?, ?, ?, ?)""",
            (entity_id, alias, alias_text.strip(), now),
        )
        return True
    except sqlite3.IntegrityError:
        return False  # alias already registered


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
