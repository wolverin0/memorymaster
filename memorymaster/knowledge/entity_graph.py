"""Entity extraction and knowledge graph.

Extracts ontology-validated entities and relationships from claim text using the configured LLM,
stores them in SQLite tables alongside the main claims DB, and enables
graph-based retrieval (find related claims via entity connections).

Ported from MemoryKing's EntityExtractor with adaptations:
- Uses MemoryMaster's configured multi-provider LLM abstraction
- Stores entity tables in the same SQLite DB as claims
- Links entities to claim IDs (not memory UUIDs)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from memorymaster.core.llm_provider import call_llm, use_call_scoped_env
from memorymaster.core.security import scan_text_for_findings
from memorymaster.knowledge.entity_registry import add_alias, resolve_or_create
from memorymaster.knowledge.ontology import Ontology, load_ontology
from memorymaster.stores._storage_shared import connect_ro, open_conn

logger = logging.getLogger(__name__)

QUERY_ENTITY_PROMPT = (
    "Extract the named entities from this query. Return JSON only: "
    '{"entities": ["entity1", "entity2"]}. '
    "Return empty array if no entities."
)

class EntityGraphNotReady(RuntimeError):
    """Raised when the versioned relational graph schema is unavailable."""


class EntityGraphProviderError(RuntimeError):
    """Raised when a graph provider response is unusable and may be retried."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _llm_chat(prompt: str, system: str = "", model: str = "", base_url: str = "") -> str:
    """Call the configured MemoryMaster provider; legacy arguments remain additive."""
    overrides = {"MEMORYMASTER_LLM_MODEL": model} if model else {}
    if base_url:
        logger.warning("EntityGraph base_url is ignored; configure the selected provider.")
    try:
        with use_call_scoped_env(overrides):
            raw = call_llm(system, prompt)
    except TimeoutError as exc:
        raise EntityGraphProviderError(
            "provider_timeout", "Graph provider invocation timed out."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - typed retry boundary
        logger.warning("LLM call failed: %s", exc)
        raise EntityGraphProviderError(
            "provider_call_failed", "Graph provider invocation failed."
        ) from exc
    if not raw or not raw.strip():
        raise EntityGraphProviderError(
            "provider_empty_response", "Graph provider returned no output."
        )
    return raw


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
        self.ontology: Ontology = load_ontology()
        self.last_diagnostics: list[str] = []

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
                    "name IN ('entities','entity_aliases','entity_edges','claim_entity_links',"
                    "'entity_edge_supports')"
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
                "entity_edge_supports",
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

    def _process_entities(
        self, data: dict, conn, *, scope: str
    ) -> tuple[dict[str, int], list[str]]:
        """Process extracted entities and return (name->id mapping, original names)."""
        entity_names = []
        entity_id_map: dict[str, int] = {}

        for ent in data.get("entities", []):
            name = (ent.get("name") or "").strip()
            if not name or len(name) < 2:
                continue
            ent_type = str(ent.get("type", "")).strip().lower()
            if ent_type not in self.ontology.entity_types:
                self.last_diagnostics.append(f"unknown_entity_type:{ent_type or 'empty'}")
                continue
            aliases = [str(alias) for alias in ent.get("aliases", []) if alias]
            ent_id = self._upsert_entity(conn, name, ent_type, aliases, scope=scope)
            if ent_id <= 0:
                continue
            entity_id_map[name.lower()] = ent_id
            entity_names.append(name)
            for alias in aliases:
                entity_id_map[alias.lower()] = ent_id

        return entity_id_map, entity_names

    def _claim_graph_metadata(self, conn, claim_id: int) -> tuple[str, bool]:
        row = conn.execute(
            """SELECT scope, status, visibility, text, subject, predicate, object_value
               FROM claims WHERE id=?""",
            (claim_id,),
        ).fetchone()
        if row is None:
            self.last_diagnostics.append("claim_missing")
            return "", False
        if row["status"] != "confirmed":
            self.last_diagnostics.append(f"claim_not_confirmed:{row['status']}")
            return str(row["scope"]), False
        values = " ".join(str(row[key] or "") for key in ("text", "subject", "predicate", "object_value"))
        sensitive = row["visibility"] == "sensitive" or bool(scan_text_for_findings(values))
        if sensitive:
            self.last_diagnostics.append("sensitive_claim")
            return str(row["scope"]), False
        support = conn.execute(
            """SELECT
                   COUNT(*) AS total,
                   SUM(CASE WHEN si.retired_at IS NULL
                                  AND ei.sensitivity='none'
                                  AND si.sensitivity='none'
                            THEN 1 ELSE 0 END) AS eligible
               FROM claim_evidence_links cel
               JOIN evidence_items ei ON ei.id=cel.evidence_item_id
               JOIN source_items si ON si.id=ei.source_item_id
               WHERE cel.claim_id=?""",
            (claim_id,),
        ).fetchone()
        total = int(support["total"] or 0)
        eligible = int(support["eligible"] or 0)
        if total == 0:
            self.last_diagnostics.append("claim_evidence_missing")
            return str(row["scope"]), False
        if eligible != total:
            self.last_diagnostics.append("claim_support_ineligible")
            return str(row["scope"]), False
        return str(row["scope"]), True

    def _validated_relations(self, data: dict) -> list[dict]:
        relations: list[dict] = []
        for row in data.get("relations", []):
            if not isinstance(row, dict):
                self.last_diagnostics.append("malformed_relation")
                continue
            relation = str(row.get("relation", "")).strip().lower()
            if relation not in self.ontology.relations:
                self.last_diagnostics.append(f"unknown_relation:{relation or 'empty'}")
                continue
            relations.append(row)
        return relations

    def _payload_values_valid(self, data: dict) -> bool:
        entities = data.get("entities")
        relations = data.get("relations")
        if not isinstance(entities, list) or not isinstance(relations, list):
            self.last_diagnostics.append("malformed_schema")
            return False
        for entity in entities:
            if not isinstance(entity, dict):
                self.last_diagnostics.append("malformed_entity")
                continue
            entity_type = str(entity.get("type", "")).strip().lower()
            if entity_type not in self.ontology.entity_types:
                self.last_diagnostics.append(
                    f"unknown_entity_type:{entity_type or 'empty'}"
                )
        self._validated_relations(data)
        return not self.last_diagnostics

    def _extract_payload(self, conn, text: str) -> dict | None:
        known = self._get_known_entity_names(limit=30, conn=conn)
        context = f"\nKnown entities: {', '.join(known)}" if known else ""
        raw = _llm_chat(text[:2000], system=self.ontology.prompt() + context)
        if not raw:
            raise EntityGraphProviderError(
                "provider_empty_response", "Graph provider returned no output."
            )
        data = _parse_json(raw)
        if data == {"entities": [], "relations": []}:
            try:
                json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
            except json.JSONDecodeError:
                self.last_diagnostics.append("malformed_json")
                return None
        if not isinstance(data, dict) or not self._payload_values_valid(data):
            return None
        return data

    def extract_and_link(self, claim_id: int, text: str) -> list[str]:
        """Extract and link ontology-valid entities from an eligible claim."""
        if not text or not isinstance(text, str):
            logger.debug("extract_and_link: empty or invalid text for claim %d", claim_id)
            return []

        text = text.strip()
        if not text:
            return []

        conn = self._connect()
        try:
            self.assert_ready(conn)
            self.last_diagnostics = []
            scope, eligible = self._claim_graph_metadata(conn, claim_id)
            if not eligible:
                return []

            data = self._extract_payload(conn, text)
            if data is None:
                return []

            entity_id_map, entity_names = self._process_entities(data, conn, scope=scope)

            for rel in self._validated_relations(data):
                src = entity_id_map.get((rel.get("source") or "").lower())
                tgt = entity_id_map.get((rel.get("target") or "").lower())
                if src and tgt and src != tgt:
                    self._upsert_edge(
                        conn,
                        src,
                        tgt,
                        str(rel["relation"]),
                        claim_id,
                        scope=scope,
                    )

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

    def _legacy_find_related_claims(self, entity_names: list[str], hops: int = 2, limit: int = 50) -> list[int]:
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
            # their claims to the top; Ebbinghaus-decayed edges (weight → floor)
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

    def _seed_ids(self, conn, entity_names: list[str]) -> list[int]:
        placeholders = ",".join("?" * len(entity_names))
        names_lower = [name.lower() for name in entity_names]
        rows = conn.execute(
            f"""SELECT DISTINCT e.id FROM entities e
                LEFT JOIN entity_aliases a ON a.entity_id=e.id
                WHERE LOWER(e.canonical_name) IN ({placeholders})
                   OR LOWER(a.original_form) IN ({placeholders})""",
            names_lower + names_lower,
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def _supported_adjacency(
        self, conn, scope_allowlist: list[str] | None
    ) -> dict[int, list[dict]]:
        clauses = [
            "c.status='confirmed'",
            "(c.valid_from IS NULL OR julianday(c.valid_from) <= julianday('now'))",
            "(c.valid_until IS NULL OR julianday(c.valid_until) > julianday('now'))",
            "COALESCE(c.visibility, 'public') <> 'sensitive'",
            """(
                EXISTS (
                    SELECT 1 FROM claim_evidence_links cel
                    JOIN evidence_items ei ON ei.id=cel.evidence_item_id
                    JOIN source_items si ON si.id=ei.source_item_id
                    WHERE cel.claim_id=c.id AND si.retired_at IS NULL
                      AND ei.sensitivity='none' AND si.sensitivity='none')
                AND NOT EXISTS (
                    SELECT 1 FROM claim_evidence_links cel
                    JOIN evidence_items ei ON ei.id=cel.evidence_item_id
                    JOIN source_items si ON si.id=ei.source_item_id
                    WHERE cel.claim_id=c.id
                      AND (si.retired_at IS NOT NULL
                           OR ei.sensitivity IS NULL OR ei.sensitivity<>'none'
                           OR si.sensitivity IS NULL OR si.sensitivity<>'none'))
            )""",
        ]
        params: list[object] = []
        if scope_allowlist is not None:
            marks = ",".join("?" * len(scope_allowlist))
            clauses.append(f"es.scope IN ({marks})")
            params.extend(scope_allowlist)
        rows = conn.execute(
            f"""SELECT es.*, ee.weight FROM entity_edge_supports es
                JOIN claims c ON c.id=es.supporting_claim_id
                JOIN entity_edges ee
                  ON ee.source_id=es.source_entity_id
                 AND ee.target_id=es.target_entity_id AND ee.relation=es.relation
                WHERE {' AND '.join(clauses)}
                ORDER BY ee.weight DESC, es.supporting_claim_id""",
            params,
        ).fetchall()
        adjacency: dict[int, list[dict]] = {}
        for row in rows:
            data = dict(row)
            adjacency.setdefault(int(row["source_entity_id"]), []).append(data)
            adjacency.setdefault(int(row["target_entity_id"]), []).append(data)
        return adjacency

    @staticmethod
    def _reachable(
        seed_ids: list[int], adjacency: dict[int, list[dict]], hops: int
    ) -> dict[int, tuple[list[dict], float]]:
        reached = {entity_id: ([], 1.0) for entity_id in seed_ids}
        frontier = list(seed_ids)
        for _ in range(max(0, hops)):
            next_frontier: list[int] = []
            for entity_id in frontier:
                path, weight = reached[entity_id]
                for edge in adjacency.get(entity_id, []):
                    source_id = int(edge["source_entity_id"])
                    target_id = int(edge["target_entity_id"])
                    neighbor = target_id if entity_id == source_id else source_id
                    candidate = weight * float(edge["weight"])
                    if neighbor in reached and reached[neighbor][1] >= candidate:
                        continue
                    reached[neighbor] = ([*path, edge], candidate)
                    next_frontier.append(neighbor)
            frontier = next_frontier
            if not frontier:
                break
        return reached

    def _claim_paths(
        self,
        conn,
        reached: dict[int, tuple[list[dict], float]],
        scope_allowlist: list[str] | None,
        limit: int,
    ) -> list[dict]:
        if not reached:
            return []
        marks = ",".join("?" * len(reached))
        clauses = [
            f"cl.entity_id IN ({marks})",
            "c.status='confirmed'",
            "(c.valid_from IS NULL OR julianday(c.valid_from) <= julianday('now'))",
            "(c.valid_until IS NULL OR julianday(c.valid_until) > julianday('now'))",
            "COALESCE(c.visibility, 'public') <> 'sensitive'",
            """(
                EXISTS (
                    SELECT 1 FROM claim_evidence_links cel
                    JOIN evidence_items ei ON ei.id=cel.evidence_item_id
                    JOIN source_items si ON si.id=ei.source_item_id
                    WHERE cel.claim_id=c.id AND si.retired_at IS NULL
                      AND ei.sensitivity='none' AND si.sensitivity='none')
                AND NOT EXISTS (
                    SELECT 1 FROM claim_evidence_links cel
                    JOIN evidence_items ei ON ei.id=cel.evidence_item_id
                    JOIN source_items si ON si.id=ei.source_item_id
                    WHERE cel.claim_id=c.id
                      AND (si.retired_at IS NOT NULL
                           OR ei.sensitivity IS NULL OR ei.sensitivity<>'none'
                           OR si.sensitivity IS NULL OR si.sensitivity<>'none'))
            )""",
        ]
        params: list[object] = list(reached)
        if scope_allowlist is not None:
            scope_marks = ",".join("?" * len(scope_allowlist))
            clauses.append(f"c.scope IN ({scope_marks})")
            params.extend(scope_allowlist)
        rows = conn.execute(
            f"""SELECT cl.claim_id, cl.entity_id FROM claim_entity_links cl
                JOIN claims c ON c.id=cl.claim_id WHERE {' AND '.join(clauses)}""",
            params,
        ).fetchall()
        best: dict[int, tuple[int, list[dict], float]] = {}
        for row in rows:
            entity_id = int(row["entity_id"])
            path, weight = reached[entity_id]
            claim_id = int(row["claim_id"])
            if claim_id not in best or weight > best[claim_id][2]:
                best[claim_id] = (entity_id, path, weight)
        ordered = sorted(best.items(), key=lambda item: (-item[1][2], item[0]))
        return [
            self._path_explanation(conn, claim_id, entity_id, path, weight)
            for claim_id, (entity_id, path, weight) in ordered[:limit]
        ]

    @staticmethod
    def _path_explanation(
        conn, claim_id: int, entity_id: int, path: list[dict], weight: float
    ) -> dict:
        entity_ids = [entity_id]
        for edge in path:
            entity_ids.extend(
                [int(edge["source_entity_id"]), int(edge["target_entity_id"])]
            )
        unique_ids = list(dict.fromkeys(entity_ids))
        marks = ",".join("?" * len(unique_ids))
        names = {
            int(row["id"]): str(row["canonical_name"])
            for row in conn.execute(
                f"SELECT id, canonical_name FROM entities WHERE id IN ({marks})",
                unique_ids,
            ).fetchall()
        }
        support_ids = list(
            dict.fromkeys(int(edge["supporting_claim_id"]) for edge in path)
        )
        citations = []
        if support_ids:
            support_marks = ",".join("?" * len(support_ids))
            citations = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT claim_id, source, locator, excerpt FROM citations
                        WHERE claim_id IN ({support_marks}) ORDER BY id""",
                    support_ids,
                ).fetchall()
            ]
        return {
            "claim_id": claim_id,
            "entity_path": [names.get(value, str(value)) for value in unique_ids],
            "relations": [edge["relation"] for edge in path],
            "supporting_claim_ids": support_ids,
            "citations": citations,
            "path_weight": weight,
        }

    def find_related_claims_explained(
        self,
        entity_names: list[str],
        *,
        hops: int = 2,
        limit: int = 50,
        scope_allowlist: list[str] | None = None,
    ) -> list[dict]:
        if not entity_names:
            return []
        conn = self._connect()
        try:
            self.assert_ready(conn)
            seeds = self._seed_ids(conn, entity_names)
            adjacency = self._supported_adjacency(conn, scope_allowlist)
            reached = self._reachable(seeds, adjacency, min(max(hops, 0), 4))
            return self._claim_paths(conn, reached, scope_allowlist, limit)
        finally:
            conn.close()

    def find_related_claims(
        self,
        entity_names: list[str],
        hops: int = 2,
        limit: int = 50,
        scope_allowlist: list[str] | None = None,
    ) -> list[int]:
        """Return trusted claim IDs rehydrated from active relational supports."""
        return [
            int(row["claim_id"])
            for row in self.find_related_claims_explained(
                entity_names,
                hops=hops,
                limit=limit,
                scope_allowlist=scope_allowlist,
            )
        ]

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

    def _upsert_entity(
        self,
        conn,
        name: str,
        ent_type: str,
        aliases: list[str],
        *,
        scope: str = "global",
    ) -> int:
        entity_id = resolve_or_create(
            conn,
            name,
            entity_type=ent_type or "concept",
            scope=scope,
        )
        if entity_id <= 0:
            return 0
        for alias in aliases:
            add_alias(conn, entity_id, alias)
        return entity_id

    def _upsert_edge(
        self,
        conn,
        source_id: int,
        target_id: int,
        relation: str,
        claim_id: int,
        *,
        scope: str | None = None,
    ) -> None:
        # Hebbian potentiation: every co-occurrence strengthens the edge
        # (weight += 0.1) and stamps last_reinforced_at = NOW. The timestamp is
        # what the Ebbinghaus decay job reads to compute elapsed-days; without it
        # decay cannot distinguish a freshly-reinforced edge from a stale one.
        claim = conn.execute(
            "SELECT scope FROM claims WHERE id=?", (claim_id,)
        ).fetchone()
        if claim is None:
            return
        resolved_scope = scope or str(claim["scope"])
        definition = self.ontology.relations.get(relation)
        if definition is not None and definition.symmetric and source_id > target_id:
            source_id, target_id = target_id, source_id
        now = datetime.now(timezone.utc).isoformat()
        support = conn.execute(
            """INSERT OR IGNORE INTO entity_edge_supports
               (source_entity_id, target_entity_id, relation, supporting_claim_id,
                scope, ontology_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                target_id,
                relation,
                claim_id,
                resolved_scope,
                self.ontology.version,
                now,
            ),
        )
        if support.rowcount == 0:
            return
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
