"""Entity extraction and knowledge graph.

Extracts named entities and relationships from claim text using an LLM,
stores them in SQLite tables alongside the main claims DB, and enables
graph-based retrieval (find related claims via entity connections).

Ported from MemoryKing's EntityExtractor with adaptations:
- Uses Ollama or any OpenAI-compatible API instead of direct OpenAI
- Stores entity tables in the same SQLite DB as claims
- Links entities to claim IDs (not memory UUIDs)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ENTITY_SYSTEM_PROMPT = (
    "Extract named entities and their relationships from the text. "
    "Return JSON only: "
    '{"entities": [{"name": "canonical name", "type": "person|org|place|product|concept|project|server|api", '
    '"aliases": ["alt name"]}], '
    '"relations": [{"source": "entity name", "target": "entity name", '
    '"relation": "works_at|located_in|owns|part_of|related_to|manages|created_by|uses|depends_on"}]}. '
    "Be precise. Only extract entities explicitly mentioned. "
    "Return empty arrays if no entities found."
)

QUERY_ENTITY_PROMPT = (
    "Extract the named entities from this query. Return JSON only: "
    '{"entities": ["entity1", "entity2"]}. '
    "Return empty array if no entities."
)

DEFAULT_OLLAMA_URL = "http://192.168.100.155:11434"
DEFAULT_MODEL = "deepseek-coder-v2:16b"


def _llm_chat(prompt: str, system: str = "", model: str = "", base_url: str = "") -> str:
    """Call an Ollama-compatible LLM. Returns raw text response."""
    url = (base_url or os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
    mdl = model or os.environ.get("ENTITY_LLM_MODEL") or DEFAULT_MODEL

    body = json.dumps({
        "model": mdl,
        "messages": [
            {"role": "system", "content": system} if system else None,
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 500},
    }).encode()
    # Filter None messages
    data = json.loads(body)
    data["messages"] = [m for m in data["messages"] if m is not None]
    body = json.dumps(data).encode()

    req = urllib.request.Request(
        f"{url}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get("message", {}).get("content", "")
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("LLM call failed: %s", exc)
        return ""


def _parse_json(raw: str) -> dict:
    """Parse JSON from LLM output, tolerating markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"entities": [], "relations": []}


class EntityGraph:
    """Entity extraction and graph storage in SQLite."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def ensure_tables(self) -> None:
        """Create entity tables if they don't exist. Idempotent - safe to call multiple times."""
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'concept',
                    aliases TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_name
                    ON entities(name COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS entity_edges (
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'related_to',
                    weight REAL NOT NULL DEFAULT 1.0,
                    claim_id INTEGER,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, target_id, relation)
                );
                CREATE TABLE IF NOT EXISTS claim_entity_links (
                    claim_id INTEGER NOT NULL,
                    entity_id TEXT NOT NULL,
                    PRIMARY KEY (claim_id, entity_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cel_entity
                    ON claim_entity_links(entity_id);
            """)
            conn.commit()
        except sqlite3.OperationalError as exc:
            logger.warning("ensure_tables already called (idempotent): %s", exc)
            conn.rollback()
        finally:
            conn.close()

    def _process_entities(self, data: dict, conn) -> dict[str, str]:
        """Process extracted entities and return name->id mapping."""
        entity_names = []
        entity_id_map: dict[str, str] = {}

        for ent in data.get("entities", []):
            name = (ent.get("name") or "").strip()
            if not name or len(name) < 2:
                continue
            ent_type = ent.get("type", "concept")
            aliases = ent.get("aliases", [])
            ent_id = self._upsert_entity(conn, name, ent_type, aliases)
            entity_id_map[name.lower()] = ent_id
            entity_names.append(name)
            for alias in aliases:
                entity_id_map[alias.lower()] = ent_id

        return entity_id_map

    def extract_and_link(self, claim_id: int, text: str) -> list[str]:
        """Extract entities from claim text, store in graph, link to claim.

        Gracefully handles empty text and missing tables.
        """
        if not text or not isinstance(text, str):
            logger.debug("extract_and_link: empty or invalid text for claim %d", claim_id)
            return []

        text = text.strip()
        if not text:
            return []

        try:
            self.ensure_tables()
        except sqlite3.OperationalError as exc:
            logger.error("Failed to ensure entity tables for claim %d: %s", claim_id, exc)
            return []

        known = self._get_known_entity_names(limit=30)
        context = f"\nKnown entities: {', '.join(known)}" if known else ""

        raw = _llm_chat(text[:2000], system=ENTITY_SYSTEM_PROMPT + context)
        if not raw:
            return []
        data = _parse_json(raw)

        conn = self._connect()
        try:
            entity_id_map = self._process_entities(data, conn)
            entity_names = list(entity_id_map.keys())

            for rel in data.get("relations", []):
                src = entity_id_map.get((rel.get("source") or "").lower())
                tgt = entity_id_map.get((rel.get("target") or "").lower())
                if src and tgt and src != tgt:
                    self._upsert_edge(conn, src, tgt, rel.get("relation", "related_to"), claim_id)

            for ent_id in set(entity_id_map.values()):
                conn.execute(
                    "INSERT OR IGNORE INTO claim_entity_links (claim_id, entity_id) VALUES (?, ?)",
                    (claim_id, ent_id),
                )
            conn.commit()
        finally:
            conn.close()

        logger.info("Extracted %d entities for claim %d", len(entity_names), claim_id)
        return entity_names

    def find_related_claims(self, entity_names: list[str], hops: int = 2, limit: int = 50) -> list[int]:
        """Graph BFS: find claim IDs related to entities.

        Returns empty list for non-existent entities or if tables don't exist.
        """
        if not entity_names:
            return []

        conn = self._connect()
        try:
            # Check if tables exist before querying
            try:
                conn.execute("SELECT 1 FROM entities LIMIT 1")
            except sqlite3.OperationalError:
                logger.debug("Entity tables don't exist yet, returning empty list")
                return []

            placeholders = ",".join("?" * len(entity_names))
            names_lower = [n.lower() for n in entity_names]
            seed_rows = conn.execute(
                f"SELECT id FROM entities WHERE LOWER(name) IN ({placeholders})",
                names_lower,
            ).fetchall()
            if not seed_rows:
                return []
            seed_ids = [r["id"] for r in seed_rows]
            ph = ",".join("?" * len(seed_ids))
            rows = conn.execute(
                f"""
                WITH RECURSIVE reachable(entity_id, depth) AS (
                    SELECT id, 0 FROM entities WHERE id IN ({ph})
                    UNION
                    SELECT e.target_id, r.depth + 1
                    FROM entity_edges e JOIN reachable r ON e.source_id = r.entity_id
                    WHERE r.depth < ?
                    UNION
                    SELECT e.source_id, r.depth + 1
                    FROM entity_edges e JOIN reachable r ON e.target_id = r.entity_id
                    WHERE r.depth < ?
                )
                SELECT DISTINCT cl.claim_id
                FROM reachable r
                JOIN claim_entity_links cl ON cl.entity_id = r.entity_id
                LIMIT ?
                """,
                seed_ids + [hops, hops, limit],
            ).fetchall()
            return [r["claim_id"] for r in rows]
        except sqlite3.OperationalError as exc:
            logger.warning("find_related_claims failed: %s", exc)
            return []
        finally:
            conn.close()

    def get_stats(self) -> dict:
        conn = self._connect()
        try:
            entities = conn.execute("SELECT COUNT(*) as c FROM entities").fetchone()["c"]
            edges = conn.execute("SELECT COUNT(*) as c FROM entity_edges").fetchone()["c"]
            links = conn.execute("SELECT COUNT(*) as c FROM claim_entity_links").fetchone()["c"]
            types = {
                r["type"]: r["cnt"]
                for r in conn.execute("SELECT type, COUNT(*) as cnt FROM entities GROUP BY type").fetchall()
            }
            return {"entities": entities, "edges": edges, "claim_links": links, "by_type": types}
        finally:
            conn.close()

    def _upsert_entity(self, conn, name: str, ent_type: str, aliases: list[str]) -> str:
        existing = conn.execute(
            "SELECT id, aliases FROM entities WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchone()
        if existing:
            current = json.loads(existing["aliases"])
            merged = list(set(current + aliases))
            if merged != current:
                conn.execute("UPDATE entities SET aliases = ? WHERE id = ?", (json.dumps(merged), existing["id"]))
            return existing["id"]
        ent_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO entities (id, name, type, aliases, created_at) VALUES (?, ?, ?, ?, ?)",
            (ent_id, name, ent_type, json.dumps(aliases), datetime.now(timezone.utc).isoformat()),
        )
        return ent_id

    def _upsert_edge(self, conn, source_id: str, target_id: str, relation: str, claim_id: int) -> None:
        conn.execute(
            """INSERT INTO entity_edges (source_id, target_id, relation, weight, claim_id, created_at)
               VALUES (?, ?, ?, 1.0, ?, ?)
               ON CONFLICT(source_id, target_id, relation)
               DO UPDATE SET weight = weight + 0.1, claim_id = ?""",
            (source_id, target_id, relation, claim_id,
             datetime.now(timezone.utc).isoformat(), claim_id),
        )

    def _get_known_entity_names(self, limit: int = 50) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT name FROM entities ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [r["name"] for r in rows]
        finally:
            conn.close()
