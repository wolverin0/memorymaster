"""Read-side query methods for SQLiteStore (get/list/find/count).

This is a mixin class for memorymaster.storage.SQLiteStore. All methods
expect to be bound to a SQLiteStore instance and rely on `self.connect()`
and `self.db_path`. Do not instantiate directly.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from memorymaster.models import (
    Citation,
    Claim,
    ClaimLink,
    Event,
)

logger = logging.getLogger(__name__)



class _ReadMixin:

    def _check_idempotency(self, conn: sqlite3.Connection, idempotency_key: str | None) -> Claim | None:
        """Check if a claim with this idempotency key already exists. Returns existing claim or None."""
        normalized_key = (idempotency_key or "").strip() or None
        if normalized_key is None:
            return None
        existing_row = conn.execute(
            "SELECT id FROM claims WHERE idempotency_key = ?",
            (normalized_key,),
        ).fetchone()
        if existing_row is not None:
            existing = self.get_claim(int(existing_row["id"]))
            if existing is None:
                raise RuntimeError("Idempotency key matched missing claim.")
            return existing
        return None


    def get_claim(self, claim_id: int, include_citations: bool = True) -> Claim | None:
        try:
            with self.connect() as conn:
                row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                # Recreate schema and try again
                logger.warning("claims table missing, reinitializing: %s", exc)
                self.init_db()
                # Recursively call get_claim to try again
                return self.get_claim(claim_id, include_citations)
            raise
        if row is None:
            return None
        claim = self._row_to_claim(row)
        if include_citations:
            claim.citations = self.list_citations(claim.id)
        return claim


    def get_claim_by_idempotency_key(self, idempotency_key: str, include_citations: bool = True) -> Claim | None:
        normalized_idempotency_key = idempotency_key.strip()
        if not normalized_idempotency_key:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM claims WHERE idempotency_key = ?",
                (normalized_idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        claim = self._row_to_claim(row)
        if include_citations:
            claim.citations = self.list_citations(claim.id)
        return claim


    def get_claim_by_human_id(self, human_id: str, include_citations: bool = True) -> Claim | None:
        """Look up a claim by its human-readable ID (e.g. ``mm-a3f8``)."""
        normalized = human_id.strip()
        if not normalized:
            return None
        with self.connect() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM claims WHERE human_id = ?",
                    (normalized,),
                ).fetchone()
            except sqlite3.OperationalError:
                # Column may not exist yet.
                return None
        if row is None:
            return None
        claim = self._row_to_claim(row)
        if include_citations:
            claim.citations = self.list_citations(claim.id)
        return claim


    def resolve_claim_id(self, identifier: str | int) -> int:
        """Resolve a numeric ID or human_id string to a numeric claim ID.

        Raises ``ValueError`` if the claim cannot be found.
        """
        if isinstance(identifier, int):
            return identifier
        raw = str(identifier).strip()
        # Try numeric first.
        try:
            return int(raw)
        except ValueError:
            pass
        # Try human_id lookup.
        claim = self.get_claim_by_human_id(raw, include_citations=False)
        if claim is not None:
            return claim.id
        raise ValueError(f"No claim found for identifier '{raw}'.")


    @staticmethod
    def _escape_fts5_query(text: str) -> str:
        """Escape a user query string for safe use in FTS5 MATCH.

        Each token is wrapped in double quotes so that FTS5 special
        characters (*, :, OR, AND, NOT, etc.) are treated as literals.
        Tokens are joined with implicit AND semantics.
        """
        tokens = text.split()
        if not tokens:
            return '""'
        escaped = ['"' + token.replace('"', '""') + '"' for token in tokens]
        return " ".join(escaped)


    @staticmethod
    def _has_fts5_table(conn: sqlite3.Connection) -> bool:
        """Check if the claims_fts virtual table exists."""
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims_fts'"
        ).fetchone()
        return row is not None


    def _build_list_clauses(
        self,
        status: str | None,
        status_in: list[str] | None,
        include_archived: bool,
        scope_allowlist: list[str] | None,
        tenant_id: str | None,
    ) -> tuple[list[str], list[object]]:
        """Build WHERE clauses and parameters for list_claims."""
        clauses: list[str] = []
        params: list[object] = []

        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)

        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        elif status_in:
            placeholders = ",".join("?" for _ in status_in)
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_in)

        if not include_archived and status != "archived":
            clauses.append("status <> 'archived'")

        if scope_allowlist:
            normalized_scopes = [scope.strip() for scope in scope_allowlist if scope and scope.strip()]
            if normalized_scopes:
                placeholders = ",".join("?" for _ in normalized_scopes)
                clauses.append(f"scope IN ({placeholders})")
                params.extend(normalized_scopes)

        return clauses, params


    def list_claims(
        self,
        *,
        status: str | None = None,
        status_in: list[str] | None = None,
        limit: int = 50,
        include_archived: bool = False,
        text_query: str | None = None,
        include_citations: bool = False,
        scope_allowlist: list[str] | None = None,
        tenant_id: str | None = None,
    ) -> list[Claim]:
        clauses, params = self._build_list_clauses(status, status_in, include_archived, scope_allowlist, tenant_id)

        fts_query = ""
        if text_query:
            fts_query = self._escape_fts5_query(text_query)

        with self.connect() as conn:
            if text_query and self._has_fts5_table(conn):
                clauses.append("c.id IN (SELECT rowid FROM claims_fts WHERE claims_fts MATCH ?)")
                params.append(fts_query)

                where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                sql = f"""
                    SELECT c.*, bm25(claims_fts) AS _fts_rank
                    FROM claims c
                    JOIN claims_fts ON claims_fts.rowid = c.id
                    {where_sql}
                    AND claims_fts MATCH ?
                    ORDER BY _fts_rank ASC, c.pinned DESC, c.confidence DESC, c.updated_at DESC, c.id DESC
                    LIMIT ?
                """
                params.append(fts_query)
                params.append(limit)
            else:
                if text_query:
                    clauses.append("(LOWER(text) LIKE ? OR LOWER(COALESCE(normalized_text, '')) LIKE ?)")
                    needle = f"%{text_query.lower()}%"
                    params.extend([needle, needle])

                where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                sql = f"""
                    SELECT * FROM claims
                    {where_sql}
                    ORDER BY pinned DESC, confidence DESC, updated_at DESC, id DESC
                    LIMIT ?
                """
                params.append(limit)

            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    # Check if database has been initialized at all
                    try:
                        table_count = conn.execute(
                            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                        ).fetchone()[0]
                    except sqlite3.OperationalError as e2:
                        # Can't even query sqlite_master - database is broken, raise the original error
                        logger.error(f"Can't check sqlite_master: {e2}")
                        raise

                    if table_count == 0:
                        # Database file exists but is empty/not initialized - raise error
                        logger.error("Database has 0 tables, raising error")
                        raise
                    else:
                        # Database has tables but claims is missing - return empty
                        # This can happen due to concurrent cleanup or corruption
                        logger.warning(f"Database has {table_count} tables but claims missing, returning empty: {exc}")
                        return []
                raise

        claims = [self._row_to_claim(row) for row in rows]
        if include_citations and claims:
            cit_map = self.list_citations_batch([c.id for c in claims])
            for claim in claims:
                claim.citations = cit_map.get(claim.id, [])
        return claims


    def list_citations(self, claim_id: int) -> list[Citation]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM citations WHERE claim_id = ? ORDER BY id ASC",
                (claim_id,),
            ).fetchall()
        return [self._row_to_citation(row) for row in rows]


    def list_citations_batch(self, claim_ids: list[int]) -> dict[int, list[Citation]]:
        """Fetch citations for multiple claims in a single query.

        Returns a dict mapping claim_id -> list of citations.
        Much faster than calling list_citations() in a loop.
        """
        if not claim_ids:
            return {}
        result: dict[int, list[Citation]] = {cid: [] for cid in claim_ids}
        # SQLite has a variable limit (~999), so batch in chunks
        chunk_size = 900
        for i in range(0, len(claim_ids), chunk_size):
            chunk = claim_ids[i:i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            with self.connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM citations WHERE claim_id IN ({placeholders}) ORDER BY claim_id, id ASC",
                    chunk,
                ).fetchall()
            for row in rows:
                cid = int(row["claim_id"])
                if cid in result:
                    result[cid].append(self._row_to_citation(row))
        return result


    def list_events(
        self,
        claim_id: int | None = None,
        limit: int = 100,
        event_type: str | None = None,
    ) -> list[Event]:
        clauses: list[str] = []
        params: list[object] = []

        if claim_id is not None:
            clauses.append("claim_id = ?")
            params.append(claim_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM events {where_sql} ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_event(row) for row in rows]


    def count_citations(self, claim_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM citations WHERE claim_id = ?", (claim_id,)).fetchone()
        return int(row["c"]) if row is not None else 0


    def count_citations_batch(self, claim_ids: list[int]) -> dict[int, int]:
        """Count citations for multiple claims in a single query."""
        if not claim_ids:
            return {}
        result = {cid: 0 for cid in claim_ids}
        chunk_size = 900
        for i in range(0, len(claim_ids), chunk_size):
            chunk = claim_ids[i:i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            with self.connect() as conn:
                rows = conn.execute(
                    f"SELECT claim_id, COUNT(*) AS c FROM citations WHERE claim_id IN ({placeholders}) GROUP BY claim_id",
                    chunk,
                ).fetchall()
            for row in rows:
                result[int(row["claim_id"])] = int(row["c"])
        return result


    def find_by_status(self, status: str, limit: int = 100, include_citations: bool = False) -> list[Claim]:
        return self.list_claims(
            status=status,
            limit=limit,
            include_archived=True,
            include_citations=include_citations,
        )


    def find_for_decay(self, limit: int = 200) -> list[Claim]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM claims
                WHERE status = 'confirmed'
                  AND pinned = 0
                ORDER BY updated_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_claim(row) for row in rows]


    def find_for_compaction(self, retain_days: int, limit: int = 500) -> list[Claim]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).replace(microsecond=0).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM claims
                WHERE status IN ('stale', 'superseded', 'conflicted')
                  AND pinned = 0
                  AND updated_at < ?
                ORDER BY updated_at ASC, id ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [self._row_to_claim(row) for row in rows]


    def find_confirmed_by_tuple(
        self,
        *,
        subject: str | None,
        predicate: str | None,
        scope: str | None,
        exclude_claim_id: int | None = None,
    ) -> list[Claim]:
        if not subject or not predicate:
            return []

        clauses = ["status = 'confirmed'", "subject = ?", "predicate = ?", "scope = ?"]
        params: list[object] = [subject, predicate, scope or "project"]
        if exclude_claim_id is not None:
            clauses.append("id <> ?")
            params.append(exclude_claim_id)

        sql = f"""
            SELECT * FROM claims
            WHERE {' AND '.join(clauses)}
            ORDER BY confidence DESC, updated_at DESC
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_claim(row) for row in rows]


    @staticmethod
    def _row_to_claim(row: sqlite3.Row) -> Claim:
        keys = row.keys()
        idempotency_key = row["idempotency_key"] if "idempotency_key" in keys else None
        human_id = row["human_id"] if "human_id" in keys else None
        tenant_id = row["tenant_id"] if "tenant_id" in keys else None
        tier = row["tier"] if "tier" in keys else "working"
        access_count = int(row["access_count"]) if "access_count" in keys else 0
        last_accessed_val = row["last_accessed"] if "last_accessed" in keys else None
        version = int(row["version"]) if "version" in keys and row["version"] is not None else 1
        wiki_article = row["wiki_article"] if "wiki_article" in keys else None
        return Claim(
            id=int(row["id"]),
            text=str(row["text"]),
            idempotency_key=idempotency_key,
            normalized_text=row["normalized_text"],
            claim_type=row["claim_type"],
            subject=row["subject"],
            predicate=row["predicate"],
            object_value=row["object_value"],
            scope=str(row["scope"]),
            volatility=str(row["volatility"]),
            status=str(row["status"]),
            confidence=float(row["confidence"]),
            pinned=bool(row["pinned"]),
            supersedes_claim_id=row["supersedes_claim_id"],
            replaced_by_claim_id=row["replaced_by_claim_id"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_validated_at=row["last_validated_at"],
            archived_at=row["archived_at"],
            human_id=human_id,
            tenant_id=tenant_id,
            tier=str(tier) if tier else "working",
            access_count=access_count,
            last_accessed=last_accessed_val,
            event_time=row["event_time"] if "event_time" in keys else None,
            valid_from=row["valid_from"] if "valid_from" in keys else None,
            valid_until=row["valid_until"] if "valid_until" in keys else None,
            source_agent=row["source_agent"] if "source_agent" in keys else None,
            visibility=row["visibility"] if "visibility" in keys else "public",
            version=version,
            wiki_article=wiki_article,
        )


    @staticmethod
    def _row_to_citation(row: sqlite3.Row) -> Citation:
        return Citation(
            id=int(row["id"]),
            claim_id=int(row["claim_id"]),
            source=str(row["source"]),
            locator=row["locator"],
            excerpt=row["excerpt"],
            created_at=str(row["created_at"]),
        )


    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            id=int(row["id"]),
            claim_id=int(row["claim_id"]) if row["claim_id"] is not None else None,
            event_type=str(row["event_type"]),
            from_status=row["from_status"],
            to_status=row["to_status"],
            details=row["details"],
            payload_json=row["payload_json"],
            created_at=str(row["created_at"]),
        )


    @staticmethod
    def _row_to_claim_link(row: sqlite3.Row) -> ClaimLink:
        return ClaimLink(
            id=int(row["id"]),
            source_id=int(row["source_id"]),
            target_id=int(row["target_id"]),
            link_type=str(row["link_type"]),
            created_at=str(row["created_at"]),
        )


    def get_derived_from_target_ids(self, candidate_ids: list[int]) -> set[int]:
        """Return the subset of *candidate_ids* that are targets of a ``derived_from`` link.

        This is a batch-optimised helper used by compact-summaries to avoid
        an N+1 query when filtering already-summarized claims.
        """
        if not candidate_ids:
            return set()
        with self.connect() as conn:
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = conn.execute(
                f"""
                SELECT DISTINCT target_id FROM claim_links
                WHERE link_type = 'derived_from'
                  AND target_id IN ({placeholders})
                """,
                candidate_ids,
            ).fetchall()
        return {row[0] if isinstance(row, (tuple, list)) else row["target_id"] for row in rows}


    def get_claim_links(self, claim_id: int) -> list[ClaimLink]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM claim_links
                WHERE source_id = ? OR target_id = ?
                ORDER BY created_at ASC
                """,
                (claim_id, claim_id),
            ).fetchall()
        return [self._row_to_claim_link(row) for row in rows]


    def get_linked_claims(self, claim_id: int, link_type: str | None = None) -> list[ClaimLink]:
        with self.connect() as conn:
            if link_type is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM claim_links
                    WHERE (source_id = ? OR target_id = ?) AND link_type = ?
                    ORDER BY created_at ASC
                    """,
                    (claim_id, claim_id, link_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM claim_links
                    WHERE source_id = ? OR target_id = ?
                    ORDER BY created_at ASC
                    """,
                    (claim_id, claim_id),
                ).fetchall()
        return [self._row_to_claim_link(row) for row in rows]


    def query_as_of(self, timestamp: str, *, limit: int = 50) -> list[Claim]:
        """Return claims whose validity window covers *timestamp*.

        A claim is considered valid at *timestamp* when:
        - valid_from is NULL or valid_from <= timestamp, AND
        - valid_until is NULL or valid_until > timestamp.

        Claims without any temporal columns are included (backward compat).
        """
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.* FROM claims c
                WHERE c.status NOT IN ('archived')
                  AND (c.valid_from IS NULL OR c.valid_from <= ?)
                  AND (c.valid_until IS NULL OR c.valid_until > ?)
                ORDER BY c.updated_at DESC
                LIMIT ?
                """,
                (timestamp, timestamp, limit),
            ).fetchall()
        claims = [self._row_to_claim(row) for row in rows]
        for claim in claims:
            claim.citations = self._load_citations(claim.id)
        return claims


    def _load_citations(self, claim_id: int) -> list[Citation]:
        """Load citations for a single claim."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM citations WHERE claim_id = ? ORDER BY id",
                (claim_id,),
            ).fetchall()
        return [self._row_to_citation(row) for row in rows]

    def traverse_relationships(
        self,
        start_claim_id: int,
        *,
        link_types: list[str] | None = None,
        max_depth: int = 3,
        direction: str = "both",
    ) -> list[dict]:
        """Traverse the claim relationship graph from a starting claim.

        Returns a list of dicts: [{"claim": Claim, "depth": int, "path": [int],
        "link_type": str}]. BFS traversal, stops at max_depth. direction can be
        "outgoing" (source→target), "incoming" (target→source), or "both".

        Inspired by GBrain's graph traversal queries — "what depends on Qdrant?"
        becomes traverse_relationships(qdrant_claim_id, link_types=["depends_on"]).
        """
        with self.connect() as conn:
            visited: set[int] = {start_claim_id}
            queue: list[tuple[int, int, list[int], str]] = []  # (claim_id, depth, path, via_link_type)

            # Seed with depth-0 neighbors
            def _get_neighbors(claim_id: int) -> list[tuple[int, str]]:
                neighbors: list[tuple[int, str]] = []
                if direction in ("outgoing", "both"):
                    q = "SELECT target_id, link_type FROM claim_links WHERE source_id = ?"
                    for row in conn.execute(q, (claim_id,)).fetchall():
                        if link_types is None or row[1] in link_types:
                            neighbors.append((row[0], row[1]))
                if direction in ("incoming", "both"):
                    q = "SELECT source_id, link_type FROM claim_links WHERE target_id = ?"
                    for row in conn.execute(q, (claim_id,)).fetchall():
                        if link_types is None or row[1] in link_types:
                            neighbors.append((row[0], row[1]))
                return neighbors

            for neighbor_id, link_type in _get_neighbors(start_claim_id):
                if neighbor_id not in visited:
                    visited.add(neighbor_id)
                    queue.append((neighbor_id, 1, [start_claim_id, neighbor_id], link_type))

            results: list[dict] = []
            while queue:
                cid, depth, path, via_type = queue.pop(0)
                claim = self.get_claim(cid, include_citations=False)
                if claim:
                    results.append({
                        "claim": claim,
                        "depth": depth,
                        "path": path,
                        "link_type": via_type,
                    })
                if depth < max_depth:
                    for neighbor_id, link_type in _get_neighbors(cid):
                        if neighbor_id not in visited:
                            visited.add(neighbor_id)
                            queue.append((neighbor_id, depth + 1, path + [neighbor_id], link_type))

        return results

