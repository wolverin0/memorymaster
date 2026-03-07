from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.embeddings import EmbeddingProvider, cosine_similarity
from memorymaster.models import (
    CLAIM_STATUSES,
    STATUS_TRANSITION_EVENT_TYPES,
    Citation,
    CitationInput,
    Claim,
    Event,
    validate_event_payload,
    validate_event_type,
    validate_transition_event_type,
)
from memorymaster.schema import load_schema_sql

EVENT_HASH_ALGO = "sha256-v1"
SQLITE_EVENTS_APPEND_ONLY_TRIGGERS = (
    "trg_events_append_only_update",
    "trg_events_append_only_delete",
)
SQLITE_CONFIRMED_TUPLE_GUARD_TRIGGERS = (
    "trg_claims_confirmed_tuple_guard_insert",
    "trg_claims_confirmed_tuple_guard_update",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SQLiteStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(load_schema_sql())
            self._ensure_claim_idempotency_schema(conn)
            self._ensure_confirmed_tuple_uniqueness_schema(conn)
            self._ensure_event_integrity_schema(conn)
            conn.commit()

    @staticmethod
    def _ensure_claim_idempotency_schema(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN idempotency_key TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_idempotency_key ON claims(idempotency_key)")

    @staticmethod
    def _ensure_confirmed_tuple_uniqueness_schema(conn: sqlite3.Connection) -> None:
        for trigger in SQLITE_CONFIRMED_TUPLE_GUARD_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.executescript(
            """
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
            """
        )
        try:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_confirmed_tuple_unique
                ON claims(subject, predicate, scope)
                WHERE status = 'confirmed'
                  AND subject IS NOT NULL
                  AND predicate IS NOT NULL
                """
            )
        except sqlite3.IntegrityError as exc:
            lowered = str(exc).lower()
            if "unique constraint failed" not in lowered:
                raise

    @staticmethod
    def _ensure_event_integrity_schema(conn: sqlite3.Connection) -> None:
        columns = {
            "prev_event_hash": "TEXT",
            "event_hash": "TEXT",
            "hash_algo": "TEXT",
        }
        for name, sql_type in columns.items():
            try:
                conn.execute(f"ALTER TABLE events ADD COLUMN {name} {sql_type}")
            except sqlite3.OperationalError as exc:
                lowered = str(exc).lower()
                if "duplicate column name" not in lowered:
                    raise
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_event_hash ON events(event_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_prev_event_hash ON events(prev_event_hash)")
        SQLiteStore._drop_events_append_only_triggers(conn)
        SQLiteStore._backfill_event_chain(conn)
        SQLiteStore._ensure_events_append_only_triggers(conn)

    @staticmethod
    def _drop_events_append_only_triggers(conn: sqlite3.Connection) -> None:
        for trigger in SQLITE_EVENTS_APPEND_ONLY_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")

    @staticmethod
    def _ensure_events_append_only_triggers(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
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
            """
        )

    @staticmethod
    def _canonical_payload(payload_json: str | None) -> str:
        if payload_json is None:
            return ""
        raw = payload_json.strip()
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _compute_event_hash(
        *,
        claim_id: int | None,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        details: str | None,
        payload_json: str | None,
        created_at: str,
        prev_event_hash: str | None,
        hash_algo: str = EVENT_HASH_ALGO,
    ) -> str:
        components = [
            hash_algo,
            str(claim_id) if claim_id is not None else "",
            event_type,
            from_status or "",
            to_status or "",
            details or "",
            SQLiteStore._canonical_payload(payload_json),
            created_at,
            prev_event_hash or "",
        ]
        material = "\x1f".join(components)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _backfill_event_chain(conn: sqlite3.Connection, *, rebuild_all: bool = False) -> int:
        rows = conn.execute(
            """
            SELECT id, claim_id, event_type, from_status, to_status, details, payload_json, created_at, event_hash, hash_algo
            FROM events
            ORDER BY id ASC
            """
        ).fetchall()
        if not rows:
            return 0

        prev_hash: str | None = None
        updated = 0
        for row in rows:
            row_hash = row["event_hash"] if "event_hash" in row.keys() else None
            row_algo = row["hash_algo"] if "hash_algo" in row.keys() else None
            if row_hash and not rebuild_all:
                prev_hash = str(row_hash)
                continue

            algo = str(row_algo) if row_algo else EVENT_HASH_ALGO
            created_at = str(row["created_at"])
            payload_json = row["payload_json"] if row["payload_json"] is None else str(row["payload_json"])
            event_hash = SQLiteStore._compute_event_hash(
                claim_id=int(row["claim_id"]) if row["claim_id"] is not None else None,
                event_type=str(row["event_type"]),
                from_status=row["from_status"],
                to_status=row["to_status"],
                details=row["details"],
                payload_json=payload_json,
                created_at=created_at,
                prev_event_hash=prev_hash,
                hash_algo=algo,
            )
            conn.execute(
                "UPDATE events SET prev_event_hash = ?, event_hash = ?, hash_algo = ? WHERE id = ?",
                (prev_hash, event_hash, algo, int(row["id"])),
            )
            prev_hash = event_hash
            updated += 1
        return updated

    @staticmethod
    def _insert_event_row(
        conn: sqlite3.Connection,
        *,
        claim_id: int | None,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        details: str | None,
        payload_json: str | None,
        created_at: str,
    ) -> int:
        prev_row = conn.execute(
            "SELECT event_hash FROM events WHERE event_hash IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_event_hash = str(prev_row["event_hash"]) if prev_row and prev_row["event_hash"] is not None else None
        event_hash = SQLiteStore._compute_event_hash(
            claim_id=claim_id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            details=details,
            payload_json=payload_json,
            created_at=created_at,
            prev_event_hash=prev_event_hash,
            hash_algo=EVENT_HASH_ALGO,
        )
        try:
            cur = conn.execute(
                """
                INSERT INTO events (
                    claim_id, event_type, from_status, to_status, details, payload_json, created_at,
                    prev_event_hash, event_hash, hash_algo
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    event_type,
                    from_status,
                    to_status,
                    details,
                    payload_json,
                    created_at,
                    prev_event_hash,
                    event_hash,
                    EVENT_HASH_ALGO,
                ),
            )
            return int(cur.lastrowid)
        except sqlite3.OperationalError as exc:
            if "no column named" not in str(exc).lower():
                raise
            cur = conn.execute(
                """
                INSERT INTO events (claim_id, event_type, from_status, to_status, details, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (claim_id, event_type, from_status, to_status, details, payload_json, created_at),
            )
            return int(cur.lastrowid)

    def create_claim(
        self,
        text: str,
        citations: list[CitationInput],
        *,
        idempotency_key: str | None = None,
        claim_type: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        object_value: str | None = None,
        scope: str = "project",
        volatility: str = "medium",
        confidence: float = 0.5,
    ) -> Claim:
        if not citations:
            raise ValueError("At least one citation is required.")
        normalized_idempotency_key = (idempotency_key or "").strip() or None
        now = utc_now()
        with self.connect() as conn:
            if normalized_idempotency_key is not None:
                existing_row = conn.execute(
                    "SELECT id FROM claims WHERE idempotency_key = ?",
                    (normalized_idempotency_key,),
                ).fetchone()
                if existing_row is not None:
                    existing = self.get_claim(int(existing_row["id"]))
                    if existing is None:
                        raise RuntimeError("Idempotency key matched missing claim.")
                    return existing

            try:
                cur = conn.execute(
                    """
                    INSERT INTO claims (
                        text, idempotency_key, normalized_text, claim_type, subject, predicate, object_value,
                        scope, volatility, status, confidence, pinned, supersedes_claim_id,
                        replaced_by_claim_id, created_at, updated_at, last_validated_at, archived_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'candidate', ?, 0, NULL, NULL, ?, ?, NULL, NULL)
                    """,
                    (
                        text,
                        normalized_idempotency_key,
                        claim_type,
                        subject,
                        predicate,
                        object_value,
                        scope,
                        volatility,
                        confidence,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                if normalized_idempotency_key is None:
                    raise
                conn.rollback()
                existing_row = conn.execute(
                    "SELECT id FROM claims WHERE idempotency_key = ?",
                    (normalized_idempotency_key,),
                ).fetchone()
                if existing_row is None:
                    raise
                existing = self.get_claim(int(existing_row["id"]))
                if existing is None:
                    raise RuntimeError("Idempotency key matched missing claim.")
                return existing

            claim_id = int(cur.lastrowid)
            for cite in citations:
                conn.execute(
                    """
                    INSERT INTO citations (claim_id, source, locator, excerpt, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (claim_id, cite.source, cite.locator, cite.excerpt, now),
                )
            ingest_payload = validate_event_payload(
                "ingest",
                {"citation_count": len(citations)},
                details="claim_ingested",
            )
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="ingest",
                from_status=None,
                to_status="candidate",
                details="claim_ingested",
                payload_json=json.dumps(ingest_payload),
                created_at=now,
            )
            conn.commit()
        claim = self.get_claim(claim_id)
        if claim is None:
            raise RuntimeError("Failed to load claim after insert.")
        return claim

    def get_claim(self, claim_id: int, include_citations: bool = True) -> Claim | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
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
    ) -> list[Claim]:
        clauses: list[str] = []
        params: list[object] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        elif status_in:
            placeholders = ",".join("?" for _ in status_in)
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_in)

        if not include_archived and status != "archived":
            clauses.append("status <> 'archived'")

        if text_query:
            clauses.append("(LOWER(text) LIKE ? OR LOWER(COALESCE(normalized_text, '')) LIKE ?)")
            needle = f"%{text_query.lower()}%"
            params.extend([needle, needle])

        if scope_allowlist:
            normalized_scopes = [scope.strip() for scope in scope_allowlist if scope and scope.strip()]
            if normalized_scopes:
                placeholders = ",".join("?" for _ in normalized_scopes)
                clauses.append(f"scope IN ({placeholders})")
                params.extend(normalized_scopes)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT * FROM claims
            {where_sql}
            ORDER BY pinned DESC, confidence DESC, updated_at DESC, id DESC
            LIMIT ?
        """
        params.append(limit)

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        claims = [self._row_to_claim(row) for row in rows]
        if include_citations:
            for claim in claims:
                claim.citations = self.list_citations(claim.id)
        return claims

    def list_citations(self, claim_id: int) -> list[Citation]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM citations WHERE claim_id = ? ORDER BY id ASC",
                (claim_id,),
            ).fetchall()
        return [self._row_to_citation(row) for row in rows]

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

    def set_normalized_text(self, claim_id: int, normalized_text: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE claims SET normalized_text = ?, updated_at = ? WHERE id = ?",
                (normalized_text, now, claim_id),
            )
            conn.commit()

    def redact_claim_payload(
        self,
        claim_id: int,
        *,
        mode: str = "redact",
        redact_claim: bool = True,
        redact_citations: bool = True,
        reason: str | None = None,
        actor: str = "system",
    ) -> dict[str, object]:
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"redact", "erase"}:
            raise ValueError("mode must be one of: redact, erase.")
        if not redact_claim and not redact_citations:
            raise ValueError("At least one of redact_claim or redact_citations must be true.")

        now = utc_now()
        details = "claim_payload_redacted" if normalized_mode == "redact" else "claim_payload_erased"
        claim_text = "[REDACTED_CLAIM_TEXT]" if normalized_mode == "redact" else "[ERASED_CLAIM_TEXT]"
        subject_value = "[REDACTED]" if normalized_mode == "redact" else None
        predicate_value = "[REDACTED]" if normalized_mode == "redact" else None
        object_value = "[REDACTED]" if normalized_mode == "redact" else None
        citation_source = "[REDACTED_SOURCE]" if normalized_mode == "redact" else "[ERASED_SOURCE]"
        citation_locator = "[REDACTED_LOCATOR]" if normalized_mode == "redact" else None
        citation_excerpt = "[REDACTED_EXCERPT]" if normalized_mode == "redact" else None

        with self.connect() as conn:
            status_row = conn.execute("SELECT status FROM claims WHERE id = ?", (claim_id,)).fetchone()
            if status_row is None:
                raise ValueError(f"Claim {claim_id} does not exist.")
            current_status = str(status_row["status"]) if status_row["status"] is not None else None

            claim_rows = 0
            citation_rows = 0

            if redact_claim:
                cur = conn.execute(
                    """
                    UPDATE claims
                    SET text = ?,
                        normalized_text = NULL,
                        subject = ?,
                        predicate = ?,
                        object_value = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (claim_text, subject_value, predicate_value, object_value, now, claim_id),
                )
                claim_rows = int(cur.rowcount)

            if redact_citations:
                cur = conn.execute(
                    """
                    UPDATE citations
                    SET source = ?, locator = ?, excerpt = ?
                    WHERE claim_id = ?
                    """,
                    (citation_source, citation_locator, citation_excerpt, claim_id),
                )
                citation_rows = int(cur.rowcount)
                if not redact_claim:
                    conn.execute(
                        "UPDATE claims SET updated_at = ? WHERE id = ?",
                        (now, claim_id),
                    )

            payload: dict[str, object] = {
                "source": str(actor or "system"),
                "mode": normalized_mode,
                "redact_claim": bool(redact_claim),
                "redact_citations": bool(redact_citations),
                "claim_rows": claim_rows,
                "citation_rows": citation_rows,
            }
            if reason and reason.strip():
                payload["reason"] = reason.strip()
            validated_payload = validate_event_payload("audit", payload, details=details)
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="audit",
                from_status=current_status,
                to_status=current_status,
                details=details,
                payload_json=json.dumps(validated_payload) if validated_payload is not None else None,
                created_at=now,
            )
            conn.commit()

        return {
            "claim_id": claim_id,
            "mode": normalized_mode,
            "redact_claim": bool(redact_claim),
            "redact_citations": bool(redact_citations),
            "claim_rows": claim_rows,
            "citation_rows": citation_rows,
            "event_details": details,
        }

    def update_claim_structure(
        self,
        claim_id: int,
        *,
        claim_type: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        object_value: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE claims
                SET claim_type = COALESCE(claim_type, ?),
                    subject = COALESCE(subject, ?),
                    predicate = COALESCE(predicate, ?),
                    object_value = COALESCE(object_value, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (claim_type, subject, predicate, object_value, now, claim_id),
            )
            conn.commit()

    def set_confidence(self, claim_id: int, confidence: float, details: str | None = None) -> None:
        bounded = max(0.0, min(1.0, confidence))
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE claims SET confidence = ?, updated_at = ? WHERE id = ?",
                (bounded, now, claim_id),
            )
            if details:
                status_row = conn.execute("SELECT status FROM claims WHERE id = ?", (claim_id,)).fetchone()
                current_status = str(status_row["status"]) if status_row else None
                self._insert_event_row(
                    conn,
                    claim_id=claim_id,
                    event_type="confidence",
                    from_status=current_status,
                    to_status=current_status,
                    details=details,
                    payload_json=None,
                    created_at=now,
                )
            conn.commit()

    def set_pinned(self, claim_id: int, pinned: bool, reason: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE claims SET pinned = ?, updated_at = ? WHERE id = ?",
                (1 if pinned else 0, now, claim_id),
            )
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="pin" if pinned else "unpin",
                from_status=None,
                to_status=None,
                details=reason,
                payload_json=None,
                created_at=now,
            )
            conn.commit()

    def apply_status_transition(
        self,
        claim: Claim,
        *,
        to_status: str,
        reason: str,
        event_type: str,
        replaced_by_claim_id: int | None = None,
    ) -> Claim:
        validated_event_type = validate_transition_event_type(event_type)
        now = utc_now()
        last_validated_at = now if to_status in {"confirmed", "stale", "conflicted"} else claim.last_validated_at
        archived_at = now if to_status == "archived" else None
        next_replaced_by = replaced_by_claim_id if replaced_by_claim_id is not None else claim.replaced_by_claim_id

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE claims
                SET status = ?, updated_at = ?, last_validated_at = ?, archived_at = ?, replaced_by_claim_id = ?
                WHERE id = ?
                """,
                (to_status, now, last_validated_at, archived_at, next_replaced_by, claim.id),
            )
            self._insert_event_row(
                conn,
                claim_id=claim.id,
                event_type=validated_event_type,
                from_status=claim.status,
                to_status=to_status,
                details=reason,
                payload_json=json.dumps({"replaced_by_claim_id": replaced_by_claim_id}) if replaced_by_claim_id else None,
                created_at=now,
            )
            conn.commit()
        updated = self.get_claim(claim.id)
        if updated is None:
            raise RuntimeError("Failed to load claim after transition.")
        return updated

    def set_supersedes(self, claim_id: int, supersedes_claim_id: int) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE claims
                SET supersedes_claim_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (supersedes_claim_id, now, claim_id),
            )
            conn.commit()

    def mark_superseded(self, old_claim_id: int, new_claim_id: int, reason: str) -> None:
        old_claim = self.get_claim(old_claim_id, include_citations=False)
        if old_claim is None:
            return
        self.apply_status_transition(
            old_claim,
            to_status="superseded",
            reason=reason,
            event_type="supersession",
            replaced_by_claim_id=new_claim_id,
        )
        self.set_supersedes(new_claim_id, old_claim_id)

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

    def delete_old_events(self, retain_days: int) -> int:
        # Events are append-only by contract; retention trim is a no-op.
        return 0

    def reconcile_integrity(self, *, fix: bool = False, limit: int = 500) -> dict[str, object]:
        report: dict[str, object] = {
            "checked_at": utc_now(),
            "fix_mode": bool(fix),
            "issues": {},
            "actions": [],
        }
        with self.connect() as conn:
            self._ensure_event_integrity_schema(conn)

            orphan_events = conn.execute(
                """
                SELECT e.id
                FROM events e
                LEFT JOIN claims c ON c.id = e.claim_id
                WHERE e.claim_id IS NOT NULL AND c.id IS NULL
                ORDER BY e.id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            orphan_citations = conn.execute(
                """
                SELECT ci.id
                FROM citations ci
                LEFT JOIN claims c ON c.id = ci.claim_id
                WHERE c.id IS NULL
                ORDER BY ci.id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            superseded_without_replacement = conn.execute(
                """
                SELECT id
                FROM claims
                WHERE status = 'superseded' AND replaced_by_claim_id IS NULL
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            dangling_replaced_by = conn.execute(
                """
                SELECT c.id
                FROM claims c
                LEFT JOIN claims n ON n.id = c.replaced_by_claim_id
                WHERE c.replaced_by_claim_id IS NOT NULL AND n.id IS NULL
                ORDER BY c.id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            dangling_supersedes = conn.execute(
                """
                SELECT c.id
                FROM claims c
                LEFT JOIN claims p ON p.id = c.supersedes_claim_id
                WHERE c.supersedes_claim_id IS NOT NULL AND p.id IS NULL
                ORDER BY c.id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            transition_issues: list[dict[str, object]] = []
            transition_placeholders = ",".join("?" for _ in STATUS_TRANSITION_EVENT_TYPES)
            transition_rows = conn.execute(
                f"""
                SELECT id, event_type, from_status, to_status
                FROM events
                WHERE event_type IN ({transition_placeholders})
                ORDER BY id ASC
                """,
                list(STATUS_TRANSITION_EVENT_TYPES),
            ).fetchall()
            for row in transition_rows:
                from_status = row["from_status"]
                to_status = row["to_status"]
                if from_status is None or to_status is None:
                    continue
                if from_status not in CLAIM_STATUSES or to_status not in CLAIM_STATUSES:
                    transition_issues.append(
                        {
                            "event_id": int(row["id"]),
                            "event_type": str(row["event_type"]),
                            "reason": "unknown_status",
                            "from_status": from_status,
                            "to_status": to_status,
                        }
                    )
                    continue
                if from_status == to_status:
                    continue
                from memorymaster.lifecycle import ALLOWED_TRANSITIONS

                if to_status not in ALLOWED_TRANSITIONS.get(str(from_status), set()):
                    transition_issues.append(
                        {
                            "event_id": int(row["id"]),
                            "event_type": str(row["event_type"]),
                            "reason": "invalid_transition",
                            "from_status": from_status,
                            "to_status": to_status,
                        }
                    )

            chain_issues: list[dict[str, object]] = []
            chain_rows = conn.execute(
                """
                SELECT id, prev_event_hash, event_hash, hash_algo
                FROM events
                ORDER BY id ASC
                """
            ).fetchall()
            expected_prev: str | None = None
            for row in chain_rows:
                row_prev = str(row["prev_event_hash"]) if row["prev_event_hash"] is not None else None
                row_hash = str(row["event_hash"]) if row["event_hash"] is not None else None
                row_algo = str(row["hash_algo"]) if row["hash_algo"] is not None else None
                if row_hash is None:
                    chain_issues.append({"event_id": int(row["id"]), "reason": "missing_hash"})
                    continue
                if row_algo not in {None, EVENT_HASH_ALGO}:
                    chain_issues.append(
                        {"event_id": int(row["id"]), "reason": "unexpected_hash_algo", "hash_algo": row_algo}
                    )
                if row_prev != expected_prev:
                    chain_issues.append(
                        {
                            "event_id": int(row["id"]),
                            "reason": "broken_prev_link",
                            "expected_prev_event_hash": expected_prev,
                            "actual_prev_event_hash": row_prev,
                        }
                    )
                expected_prev = row_hash

            issues = {
                "orphan_events": [int(row["id"]) for row in orphan_events],
                "orphan_citations": [int(row["id"]) for row in orphan_citations],
                "superseded_without_replacement": [int(row["id"]) for row in superseded_without_replacement],
                "dangling_replaced_by": [int(row["id"]) for row in dangling_replaced_by],
                "dangling_supersedes": [int(row["id"]) for row in dangling_supersedes],
                "transition_issues": transition_issues[:limit],
                "hash_chain_issues": chain_issues[:limit],
            }
            report["issues"] = issues
            report["summary"] = {
                key: (len(value) if isinstance(value, list) else 0)
                for key, value in issues.items()
            }

            actions: list[dict[str, object]] = []
            if fix:
                if issues["orphan_citations"]:
                    placeholders = ",".join("?" for _ in issues["orphan_citations"])
                    cur = conn.execute(f"DELETE FROM citations WHERE id IN ({placeholders})", issues["orphan_citations"])
                    actions.append({"action": "delete_orphan_citations", "rows": int(cur.rowcount)})
                if issues["orphan_events"]:
                    actions.append(
                        {
                            "action": "skip_delete_orphan_events_append_only",
                            "rows": 0,
                            "reason": "events table is append-only",
                        }
                    )
                if issues["dangling_replaced_by"]:
                    placeholders = ",".join("?" for _ in issues["dangling_replaced_by"])
                    cur = conn.execute(
                        f"UPDATE claims SET replaced_by_claim_id = NULL WHERE id IN ({placeholders})",
                        issues["dangling_replaced_by"],
                    )
                    actions.append({"action": "clear_dangling_replaced_by", "rows": int(cur.rowcount)})
                if issues["dangling_supersedes"]:
                    placeholders = ",".join("?" for _ in issues["dangling_supersedes"])
                    cur = conn.execute(
                        f"UPDATE claims SET supersedes_claim_id = NULL WHERE id IN ({placeholders})",
                        issues["dangling_supersedes"],
                    )
                    actions.append({"action": "clear_dangling_supersedes", "rows": int(cur.rowcount)})
                if issues["hash_chain_issues"]:
                    actions.append(
                        {
                            "action": "skip_rebuild_event_hash_chain_append_only",
                            "rows": 0,
                            "reason": "events table is append-only",
                        }
                    )
                conn.commit()
            report["actions"] = actions
        return report

    def record_event(
        self,
        *,
        claim_id: int | None,
        event_type: str,
        from_status: str | None = None,
        to_status: str | None = None,
        details: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        validated_event_type = validate_event_type(event_type)
        validated_payload = validate_event_payload(
            validated_event_type,
            payload,
            details=details,
        )
        now = utc_now()
        payload_json = json.dumps(validated_payload) if validated_payload is not None else None
        with self.connect() as conn:
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type=validated_event_type,
                from_status=from_status,
                to_status=to_status,
                details=details,
                payload_json=payload_json,
                created_at=now,
            )
            conn.commit()

    def upsert_embeddings(self, claims: list[Claim], provider: EmbeddingProvider) -> int:
        if not claims:
            return 0
        now = utc_now()
        rows = []
        for claim in claims:
            text = " ".join(
                part
                for part in [claim.text, claim.normalized_text or "", claim.subject or "", claim.object_value or ""]
                if part
            )
            embedding = provider.embed(text)
            rows.append((claim.id, provider.model, json.dumps(embedding), now))
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO claim_embeddings (claim_id, model, embedding_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(claim_id) DO UPDATE SET
                    model = excluded.model,
                    embedding_json = excluded.embedding_json,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def vector_scores(
        self,
        query_text: str,
        claims: list[Claim],
        provider: EmbeddingProvider,
    ) -> dict[int, float]:
        if not claims:
            return {}
        self.upsert_embeddings(claims, provider)
        query_vec = provider.embed(query_text)
        claim_ids = [c.id for c in claims]
        placeholders = ",".join("?" for _ in claim_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT claim_id, embedding_json FROM claim_embeddings WHERE claim_id IN ({placeholders})",
                claim_ids,
            ).fetchall()
        scores: dict[int, float] = {}
        for row in rows:
            emb = json.loads(str(row["embedding_json"]))
            sim = cosine_similarity(query_vec, emb)
            scores[int(row["claim_id"])] = max(0.0, min(1.0, (sim + 1.0) / 2.0))
        return scores

    @staticmethod
    def _row_to_claim(row: sqlite3.Row) -> Claim:
        idempotency_key = row["idempotency_key"] if "idempotency_key" in row.keys() else None
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
