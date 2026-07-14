"""Converge the entity registry and relational graph on integer entity IDs."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

VERSION = 13
DESCRIPTION = "Canonical entity registry and relational graph"

ENTITY_SCHEMA_CONTRACT = {
    "entities": ("id", "canonical_name", "entity_type", "scope"),
    "entity_aliases": ("id", "entity_id", "alias", "variant_key"),
    "entity_edges": ("source_id", "target_id", "relation", "claim_id"),
    "claim_entity_links": ("claim_id", "entity_id"),
}

_NORMALIZE_RE = re.compile(r"[\s_\-\.]+")

_SQLITE_REGISTRY_DDL = """
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
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    variant_key TEXT NOT NULL DEFAULT '',
    original_form TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(entity_id, variant_key)
);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_alias ON entity_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity_id ON entity_aliases(entity_id);
"""

_SQLITE_GRAPH_DDL = """
CREATE TABLE entity_edges (
    source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'related_to',
    weight REAL NOT NULL DEFAULT 1.0,
    claim_id INTEGER REFERENCES claims(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    last_reinforced_at TEXT,
    PRIMARY KEY (source_id, target_id, relation)
);
CREATE TABLE claim_entity_links (
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (claim_id, entity_id)
);
CREATE INDEX idx_cel_entity ON claim_entity_links(entity_id);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _columns(conn, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _normalized(value: str) -> str:
    return _NORMALIZE_RE.sub("-", value.strip().lower()).strip("-")[:200]


def _variant(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())[:200]


def _insert_alias(conn, entity_id: int, value: str, created_at: str) -> None:
    alias = _normalized(value)
    if not alias:
        return
    conn.execute(
        """INSERT OR IGNORE INTO entity_aliases
               (entity_id, alias, variant_key, original_form, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (entity_id, alias, _variant(value), value.strip()[:200], created_at),
    )


def _rename_legacy_graph(conn) -> tuple[str | None, str | None]:
    edge_table = None
    link_table = None
    if _table_exists(conn, "entity_edges"):
        conn.execute("ALTER TABLE entity_edges RENAME TO entity_edges_legacy_0013")
        edge_table = "entity_edges_legacy_0013"
    if _table_exists(conn, "claim_entity_links"):
        conn.execute(
            "ALTER TABLE claim_entity_links RENAME TO claim_entity_links_legacy_0013"
        )
        link_table = "claim_entity_links_legacy_0013"
    conn.execute("DROP INDEX IF EXISTS idx_cel_entity")
    return edge_table, link_table


def _migrate_legacy_entities(conn) -> dict[str, int]:
    if not _table_exists(conn, "entities"):
        conn.executescript(_SQLITE_REGISTRY_DDL)
        return {}
    columns = _columns(conn, "entities")
    if "canonical_name" in columns:
        required = {
            "id",
            "canonical_name",
            "entity_type",
            "scope",
            "created_at",
            "updated_at",
        }
        if not required <= columns:
            raise RuntimeError(
                "Entity migration found an incomplete canonical registry."
            )
        conn.executescript(_SQLITE_REGISTRY_DDL)
        return {str(row[0]): int(row[0]) for row in conn.execute("SELECT id FROM entities")}
    if not {"id", "name", "type", "aliases", "created_at"} <= columns:
        raise RuntimeError("Entity migration found an unsupported entities table shape.")

    conn.execute("ALTER TABLE entities RENAME TO entities_legacy_0013")
    conn.executescript(_SQLITE_REGISTRY_DDL)
    mapping: dict[str, int] = {}
    rows = conn.execute(
        "SELECT id, name, type, aliases, created_at FROM entities_legacy_0013 ORDER BY name"
    ).fetchall()
    for old_id, name, entity_type, aliases_json, created_at in rows:
        stamp = str(created_at or _utc_now())
        cur = conn.execute(
            """INSERT INTO entities
                   (canonical_name, entity_type, scope, created_at, updated_at)
               VALUES (?, ?, 'global', ?, ?)""",
            (str(name), str(entity_type or "unknown"), stamp, stamp),
        )
        entity_id = int(cur.lastrowid)
        mapping[str(old_id)] = entity_id
        _insert_alias(conn, entity_id, str(name), stamp)
        try:
            aliases = json.loads(str(aliases_json or "[]"))
        except json.JSONDecodeError:
            aliases = []
        for alias in aliases if isinstance(aliases, list) else []:
            _insert_alias(conn, entity_id, str(alias), stamp)
    return mapping


def _migrate_legacy_aliases(conn) -> None:
    if not _table_exists(conn, "entity_aliases"):
        conn.executescript(_SQLITE_REGISTRY_DDL)
        return
    columns = _columns(conn, "entity_aliases")
    required = {"id", "entity_id", "alias", "original_form", "created_at"}
    if not required <= columns:
        raise RuntimeError("Entity migration found an unsupported alias table shape.")
    fk_targets = {
        str(row[2]) for row in conn.execute("PRAGMA foreign_key_list(entity_aliases)")
    }
    if "variant_key" in columns and fk_targets == {"entities"}:
        return

    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_entity_aliases_alias;
        DROP INDEX IF EXISTS idx_entity_aliases_entity_id;
        ALTER TABLE entity_aliases RENAME TO entity_aliases_legacy_0013;
        """
    )
    conn.executescript(_SQLITE_REGISTRY_DDL)
    variant_expr = "variant_key" if "variant_key" in columns else "NULL"
    rows = conn.execute(
        "SELECT entity_id, alias, original_form, created_at, "
        f"{variant_expr} FROM entity_aliases_legacy_0013 ORDER BY id"
    ).fetchall()
    for entity_id, alias, original_form, created_at, variant_key in rows:
        display = str(original_form or alias or "")
        conn.execute(
            """INSERT OR IGNORE INTO entity_aliases
                   (entity_id, alias, variant_key, original_form, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entity_id, alias, variant_key or _variant(display), display, created_at),
        )
    conn.execute("DROP TABLE entity_aliases_legacy_0013")


def _claim_exists(conn, claim_id: object) -> bool:
    if claim_id is None or not _table_exists(conn, "claims"):
        return claim_id is None
    return conn.execute("SELECT 1 FROM claims WHERE id = ?", (claim_id,)).fetchone() is not None


def _migrate_graph_rows(conn, mapping: dict[str, int], edge_table: str | None, link_table: str | None) -> None:
    conn.executescript(_SQLITE_GRAPH_DDL)
    if edge_table:
        edge_columns = _columns(conn, edge_table)
        reinforced = (
            "last_reinforced_at"
            if "last_reinforced_at" in edge_columns
            else "NULL AS last_reinforced_at"
        )
        rows = conn.execute(
            f"SELECT source_id, target_id, relation, weight, claim_id, created_at, "
            f"{reinforced} FROM {edge_table}"
        ).fetchall()
        for source, target, relation, weight, claim_id, created_at, reinforced in rows:
            source_id = mapping.get(str(source))
            target_id = mapping.get(str(target))
            if source_id is None or target_id is None or not _claim_exists(conn, claim_id):
                raise RuntimeError("Entity migration refused orphaned legacy graph rows.")
            conn.execute(
                """INSERT INTO entity_edges VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_id, target_id, relation) DO NOTHING""",
                (source_id, target_id, relation, weight, claim_id, created_at, reinforced),
            )
    if link_table:
        for claim_id, entity_id in conn.execute(
            f"SELECT claim_id, entity_id FROM {link_table}"
        ).fetchall():
            mapped = mapping.get(str(entity_id))
            if mapped is None or not _claim_exists(conn, claim_id):
                raise RuntimeError("Entity migration refused orphaned legacy claim links.")
            conn.execute(
                "INSERT OR IGNORE INTO claim_entity_links VALUES (?, ?)",
                (claim_id, mapped),
            )


def _add_claim_entity_reference(conn) -> None:
    if not _table_exists(conn, "claims") or "entity_id" in _columns(conn, "claims"):
        return
    conn.execute(
        "ALTER TABLE claims ADD COLUMN entity_id INTEGER REFERENCES entities(id) ON DELETE SET NULL"
    )


def apply_sqlite(conn) -> None:
    edge_table, link_table = _rename_legacy_graph(conn)
    mapping = _migrate_legacy_entities(conn)
    _migrate_legacy_aliases(conn)
    _add_claim_entity_reference(conn)
    _migrate_graph_rows(conn, mapping, edge_table, link_table)
    for table in (edge_table, link_table, "entities_legacy_0013"):
        if table:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"Entity migration produced {len(violations)} FK violation(s).")
    conn.commit()


_POSTGRES_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS entities (
        id BIGSERIAL PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE,
        entity_type TEXT NOT NULL DEFAULT 'unknown', scope TEXT NOT NULL DEFAULT 'global',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS entity_aliases (
        id BIGSERIAL PRIMARY KEY, entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        alias TEXT NOT NULL, variant_key TEXT NOT NULL DEFAULT '', original_form TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(entity_id, variant_key))""",
    "CREATE INDEX IF NOT EXISTS idx_entity_aliases_alias ON entity_aliases(alias)",
    "CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity_id ON entity_aliases(entity_id)",
    "ALTER TABLE claims ADD COLUMN IF NOT EXISTS entity_id BIGINT REFERENCES entities(id) ON DELETE SET NULL",
    """CREATE TABLE IF NOT EXISTS entity_edges (
        source_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        target_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        relation TEXT NOT NULL DEFAULT 'related_to', weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
        claim_id BIGINT REFERENCES claims(id) ON DELETE SET NULL, created_at TEXT NOT NULL,
        last_reinforced_at TEXT, PRIMARY KEY(source_id, target_id, relation))""",
    """CREATE TABLE IF NOT EXISTS claim_entity_links (
        claim_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
        entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        PRIMARY KEY(claim_id, entity_id))""",
    "CREATE INDEX IF NOT EXISTS idx_cel_entity ON claim_entity_links(entity_id)",
)


def apply_postgres(conn) -> None:
    with conn.cursor() as cur:
        for statement in _POSTGRES_STATEMENTS:
            cur.execute(statement)
    conn.commit()
