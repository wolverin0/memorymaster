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
from datetime import datetime, timezone

from memorymaster.knowledge.entity_registry import add_alias, resolve_or_create
from memorymaster.stores._storage_shared import connect_ro, open_conn

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

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "deepseek-coder-v2:16b"


class EntityGraphNotReady(RuntimeError):
    """Raised when the versioned relational graph schema is unavailable."""


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

    def __init__(self, db_path: str, *, read_only: bool = False) -> None:
        self.db_path = db_path
        self.read_only = bool(read_only)
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        if self.db_path.startswith(("postgres://", "postgresql://")):
            raise EntityGraphNotReady(
                "Postgres entity extraction is not enabled; the canonical schema "
                "is migration-ready but the runtime adapter remains SQLite-only."
            )
        return connect_ro(self.db_path) if self.read_only else open_conn(self.db_path)

    def assert_ready(self, conn: sqlite3.Connection | None = None) -> None:
        """Validate the migrated schema without creating or altering objects."""
        if self._schema_ready:
            return
        own_conn = conn is None
        try:
            conn = conn or self._connect()
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND "
                    "name IN ('entities','entity_aliases','entity_edges','claim_entity_links')"
                )
            }
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(entities)")
            }
            if tables != {
                "entities",
                "entity_aliases",
                "entity_edges",
                "claim_entity_links",
            } or not {"id", "canonical_name", "entity_type", "scope"} <= columns:
                raise EntityGraphNotReady(
                    "Entity graph schema is not ready; run `memorymaster init-db` "
                    "with a schema-administration connection."
                )
            self._schema_ready = True
        except sqlite3.OperationalError as exc:
            raise EntityGraphNotReady(
                "Entity graph schema is not ready; run `memorymaster init-db` "
                "with a schema-administration connection."
            ) from exc
        finally:
            if own_conn and conn is not None:
                conn.close()

    def ensure_tables(self, conn: sqlite3.Connection | None = None) -> None:
        """Compatibility admin entrypoint backed only by immutable migrations."""
        if self._schema_ready:
            return
        own_conn = conn is None
        conn = conn or self._connect()
        try:
            from memorymaster.knowledge.entity_registry import ensure_entity_schema

            ensure_entity_schema(conn)
            self.assert_ready(conn)
        finally:
            if own_conn:
                conn.close()

    def _process_entities(self, data: dict, conn) -> tuple[dict[str, int], list[str]]:
        """Process extracted entities and return (name->id mapping, original names)."""
        entity_names = []
        entity_id_map: dict[str, int] = {}

        for ent in data.get("entities", []):
            name = (ent.get("name") or "").strip()
            if not name or len(name) < 2:
                continue
            ent_type = ent.get("type", "concept")
            aliases = [str(alias) for alias in ent.get("aliases", []) if alias]
            ent_id = self._upsert_entity(conn, name, ent_type, aliases)
            if ent_id <= 0:
                continue
            entity_id_map[name.lower()] = ent_id
            entity_names.append(name)
            for alias in aliases:
                entity_id_map[alias.lower()] = ent_id

        return entity_id_map, entity_names

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

        conn = self._connect()
        try:
            self.assert_ready(conn)

            known = self._get_known_entity_names(limit=30, conn=conn)
            context = f"\nKnown entities: {', '.join(known)}" if known else ""

            raw = _llm_chat(text[:2000], system=ENTITY_SYSTEM_PROMPT + context)
            if not raw:
                return []
            data = _parse_json(raw)

            entity_id_map, entity_names = self._process_entities(data, conn)

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
        """Graph BFS: find claim IDs related to canonical names or aliases."""
        if not entity_names:
            return []

        conn = self._connect()
        try:
            self.assert_ready(conn)
            placeholders = ",".join("?" * len(entity_names))
            names_lower = [n.lower() for n in entity_names]
            seed_rows = conn.execute(
                f"""SELECT DISTINCT e.id
                    FROM entities e
                    LEFT JOIN entity_aliases a ON a.entity_id = e.id
                    WHERE LOWER(e.canonical_name) IN ({placeholders})
                       OR LOWER(a.original_form) IN ({placeholders})""",
                names_lower + names_lower,
            ).fetchall()
            if not seed_rows:
                return []
            seed_ids = [r["id"] for r in seed_rows]
            ph = ",".join("?" * len(seed_ids))
            # Propagate edge weight along the traversal: a seed entity starts at
            # path_weight 1.0, and each hop multiplies in the traversed edge's
            # weight. Hebbian-strengthened edges (high weight) therefore pull
            # their claims to the top; Ebbinghaus-decayed edges (weight → floor)
            # sink. We MAX(path_weight) per reachable entity, sum across the
            # entities a claim links to, and return claim_ids ordered by that
            # accumulated weight DESC so the caller's scoring sees the strongest
            # graph paths first. Return type stays list[int] for callers.
            rows = conn.execute(
                f"""
                WITH RECURSIVE reachable(entity_id, depth, path_weight) AS (
                    SELECT id, 0, 1.0 FROM entities WHERE id IN ({ph})
                    UNION
                    SELECT e.target_id, r.depth + 1, r.path_weight * e.weight
                    FROM entity_edges e JOIN reachable r ON e.source_id = r.entity_id
                    WHERE r.depth < ?
                    UNION
                    SELECT e.source_id, r.depth + 1, r.path_weight * e.weight
                    FROM entity_edges e JOIN reachable r ON e.target_id = r.entity_id
                    WHERE r.depth < ?
                ),
                best(entity_id, w) AS (
                    SELECT entity_id, MAX(path_weight) FROM reachable GROUP BY entity_id
                )
                SELECT cl.claim_id AS claim_id, SUM(b.w) AS total_weight
                FROM best b
                JOIN claim_entity_links cl ON cl.entity_id = b.entity_id
                GROUP BY cl.claim_id
                ORDER BY total_weight DESC, cl.claim_id ASC
                LIMIT ?
                """,
                seed_ids + [hops, hops, limit],
            ).fetchall()
            return [r["claim_id"] for r in rows]
        finally:
            conn.close()

    def get_stats(self) -> dict:
        conn = self._connect()
        try:
            self.assert_ready(conn)
            entities = conn.execute("SELECT COUNT(*) as c FROM entities").fetchone()["c"]
            edges = conn.execute("SELECT COUNT(*) as c FROM entity_edges").fetchone()["c"]
            links = conn.execute("SELECT COUNT(*) as c FROM claim_entity_links").fetchone()["c"]
            types = {
                r["entity_type"]: r["cnt"]
                for r in conn.execute(
                    "SELECT entity_type, COUNT(*) as cnt FROM entities GROUP BY entity_type"
                ).fetchall()
            }
            return {"entities": entities, "edges": edges, "claim_links": links, "by_type": types}
        finally:
            conn.close()

    def _upsert_entity(self, conn, name: str, ent_type: str, aliases: list[str]) -> int:
        entity_id = resolve_or_create(
            conn,
            name,
            entity_type=ent_type or "concept",
            scope="global",
        )
        if entity_id <= 0:
            return 0
        for alias in aliases:
            add_alias(conn, entity_id, alias)
        return entity_id

    def _upsert_edge(self, conn, source_id: int, target_id: int, relation: str, claim_id: int) -> None:
        # Hebbian potentiation: every co-occurrence strengthens the edge
        # (weight += 0.1) and stamps last_reinforced_at = NOW. The timestamp is
        # what the Ebbinghaus decay job reads to compute elapsed-days; without it
        # decay cannot distinguish a freshly-reinforced edge from a stale one.
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO entity_edges
                   (source_id, target_id, relation, weight, claim_id, created_at, last_reinforced_at)
               VALUES (?, ?, ?, 1.0, ?, ?, ?)
               ON CONFLICT(source_id, target_id, relation)
               DO UPDATE SET weight = weight + 0.1, claim_id = ?, last_reinforced_at = ?""",
            (source_id, target_id, relation, claim_id, now, now, claim_id, now),
        )

    def _get_known_entity_names(
        self, limit: int = 50, conn: sqlite3.Connection | None = None
    ) -> list[str]:
        own_conn = conn is None
        conn = conn or self._connect()
        try:
            self.assert_ready(conn)
            rows = conn.execute(
                "SELECT canonical_name FROM entities ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [r["canonical_name"] for r in rows]
        finally:
            if own_conn:
                conn.close()
