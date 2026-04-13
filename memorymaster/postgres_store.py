from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from memorymaster.embeddings import EmbeddingProvider, cosine_similarity
from memorymaster.models import (
    CLAIM_LINK_TYPES,
    CLAIM_STATUSES,
    STATUS_TRANSITION_EVENT_TYPES,
    Citation,
    CitationInput,
    Claim,
    ClaimLink,
    Event,
    validate_event_payload,
    validate_event_type,
    validate_transition_event_type,
)
from memorymaster.retry import connect_with_retry
from memorymaster.storage import EVENT_HASH_ALGO, SQLiteStore, generate_top_level_human_id

POSTGRES_EVENTS_APPEND_ONLY_TRIGGERS = (
    "trg_events_append_only_update",
    "trg_events_append_only_delete",
)
POSTGRES_CONFIRMED_TUPLE_GUARD_TRIGGER = "trg_claims_confirmed_tuple_guard"


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class PostgresStore(SQLiteStore):
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._psycopg = None
        self._vector_table_available: bool | None = None

    def _load_psycopg(self):
        if self._psycopg is None:
            try:
                import psycopg  # type: ignore
                from psycopg.rows import dict_row  # type: ignore
                from psycopg.types.json import Jsonb  # type: ignore
            except Exception as exc:  # pragma: no cover
                raise RuntimeError(
                    "Postgres backend requires psycopg. Install with: pip install 'memorymaster[postgres]'"
                ) from exc
            self._psycopg = (psycopg, dict_row, Jsonb)
        return self._psycopg

    def connect(self):
        psycopg, dict_row, _ = self._load_psycopg()

        def _open():
            return psycopg.connect(self.dsn, row_factory=dict_row)

        return connect_with_retry(_open)

    def init_db(self) -> None:
        from memorymaster.schema import load_schema_postgres_sql

        sql = load_schema_postgres_sql()
        statements = self._split_sql_statements(sql)
        with self.connect() as conn, conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            self._ensure_confirmed_tuple_uniqueness_schema(conn)
            self._ensure_event_integrity_schema(conn)
            self._ensure_claim_links_schema(conn)
            self._ensure_human_id_schema(conn)
            self._ensure_tenant_id_schema(conn)
            self._ensure_binding_schema(conn)

    @staticmethod
    def _canonical_payload(payload: object | None) -> str:
        if payload is None:
            return ""
        if isinstance(payload, str):
            raw = payload.strip()
            if not raw:
                return ""
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _compute_event_hash(
        *,
        claim_id: int | None,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        details: str | None,
        payload: object | None,
        created_at: datetime,
        prev_event_hash: str | None,
        hash_algo: str = EVENT_HASH_ALGO,
    ) -> str:
        created_iso = created_at.replace(microsecond=0).isoformat()
        components = [
            hash_algo,
            str(claim_id) if claim_id is not None else "",
            event_type,
            from_status or "",
            to_status or "",
            details or "",
            PostgresStore._canonical_payload(payload),
            created_iso,
            prev_event_hash or "",
        ]
        material = "\x1f".join(components)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _ensure_event_integrity_schema(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS prev_event_hash TEXT")
            cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS event_hash TEXT")
            cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS hash_algo TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_event_hash ON events(event_hash)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_prev_event_hash ON events(prev_event_hash)")
            self._drop_events_append_only_triggers(cur)
            self._backfill_event_chain(conn)
            self._ensure_events_append_only_rules(cur)

    def _ensure_confirmed_tuple_uniqueness_schema(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE OR REPLACE FUNCTION memorymaster_claims_confirmed_tuple_guard()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    IF NEW.status = 'confirmed'
                       AND NEW.subject IS NOT NULL
                       AND NEW.predicate IS NOT NULL
                       AND EXISTS (
                           SELECT 1
                           FROM claims c
                           WHERE c.status = 'confirmed'
                             AND c.subject = NEW.subject
                             AND c.predicate = NEW.predicate
                             AND c.scope = NEW.scope
                             AND (TG_OP = 'INSERT' OR c.id <> NEW.id)
                       ) THEN
                        RAISE EXCEPTION 'only one confirmed claim is allowed per (subject,predicate,scope)'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN NEW;
                END;
                $$;
                """
            )
            cur.execute(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_trigger
                        WHERE tgname = '{POSTGRES_CONFIRMED_TUPLE_GUARD_TRIGGER}'
                          AND tgrelid = 'claims'::regclass
                    ) THEN
                        CREATE TRIGGER {POSTGRES_CONFIRMED_TUPLE_GUARD_TRIGGER}
                        BEFORE INSERT OR UPDATE OF status, subject, predicate, scope ON claims
                        FOR EACH ROW
                        EXECUTE FUNCTION memorymaster_claims_confirmed_tuple_guard();
                    END IF;
                END
                $$;
                """
            )
            self._try_create_confirmed_tuple_unique_index(cur)

    @staticmethod
    def _try_create_confirmed_tuple_unique_index(cur) -> None:
        savepoint = "sp_claims_confirmed_tuple_unique_idx"
        cur.execute(f"SAVEPOINT {savepoint}")
        try:
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_confirmed_tuple_unique
                ON claims(subject, predicate, scope)
                WHERE status = 'confirmed'
                  AND subject IS NOT NULL
                  AND predicate IS NOT NULL
                """
            )
        except Exception as exc:
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
            lowered = str(exc).lower()
            if (
                "could not create unique index" in lowered
                or "duplicate key value" in lowered
                or "is duplicated" in lowered
            ):
                return
            raise
        cur.execute(f"RELEASE SAVEPOINT {savepoint}")

    @staticmethod
    def _drop_events_append_only_triggers(cur) -> None:
        for trigger in POSTGRES_EVENTS_APPEND_ONLY_TRIGGERS:
            cur.execute(f"DROP TRIGGER IF EXISTS {trigger} ON events")

    @staticmethod
    def _ensure_events_append_only_rules(cur) -> None:
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION memorymaster_events_append_only_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'events table is append-only; % is not allowed', TG_OP;
            END;
            $$;
            """
        )
        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgname = 'trg_events_append_only_update'
                      AND tgrelid = 'events'::regclass
                ) THEN
                    CREATE TRIGGER trg_events_append_only_update
                    BEFORE UPDATE ON events
                    FOR EACH ROW
                    EXECUTE FUNCTION memorymaster_events_append_only_guard();
                END IF;
            END
            $$;
            """
        )
        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgname = 'trg_events_append_only_delete'
                      AND tgrelid = 'events'::regclass
                ) THEN
                    CREATE TRIGGER trg_events_append_only_delete
                    BEFORE DELETE ON events
                    FOR EACH ROW
                    EXECUTE FUNCTION memorymaster_events_append_only_guard();
                END IF;
            END
            $$;
            """
        )

    def _backfill_event_chain(self, conn, *, rebuild_all: bool = False) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, claim_id, event_type, from_status, to_status, details, payload_json, created_at, event_hash, hash_algo
                FROM events
                ORDER BY id ASC
                """
            )
            rows = cur.fetchall()
            if not rows:
                return 0

            updated = 0
            prev_hash: str | None = None
            for row in rows:
                row_hash = row.get("event_hash")
                row_algo = row.get("hash_algo")
                if row_hash and not rebuild_all:
                    prev_hash = str(row_hash)
                    continue

                algo = str(row_algo) if row_algo else EVENT_HASH_ALGO
                payload = row.get("payload_json")
                created_at = row["created_at"]
                if not isinstance(created_at, datetime):
                    created_at = datetime.fromisoformat(str(created_at))
                event_hash = self._compute_event_hash(
                    claim_id=int(row["claim_id"]) if row["claim_id"] is not None else None,
                    event_type=str(row["event_type"]),
                    from_status=self._as_text(row["from_status"]),
                    to_status=self._as_text(row["to_status"]),
                    details=self._as_text(row["details"]),
                    payload=payload,
                    created_at=created_at,
                    prev_event_hash=prev_hash,
                    hash_algo=algo,
                )
                cur.execute(
                    "UPDATE events SET prev_event_hash = %s, event_hash = %s, hash_algo = %s WHERE id = %s",
                    (prev_hash, event_hash, algo, int(row["id"])),
                )
                updated += 1
                prev_hash = event_hash
            return updated

    def _insert_event_row(
        self,
        conn,
        *,
        claim_id: int | None,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        details: str | None,
        payload: dict[str, object] | None,
        created_at: datetime,
    ) -> int:
        _, _, Jsonb = self._load_psycopg()
        with conn.cursor() as cur:
            cur.execute("SELECT event_hash FROM events WHERE event_hash IS NOT NULL ORDER BY id DESC LIMIT 1")
            prev_row = cur.fetchone()
            prev_event_hash = str(prev_row["event_hash"]) if prev_row and prev_row.get("event_hash") else None
            event_hash = self._compute_event_hash(
                claim_id=claim_id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                details=details,
                payload=payload,
                created_at=created_at,
                prev_event_hash=prev_event_hash,
                hash_algo=EVENT_HASH_ALGO,
            )
            cur.execute(
                """
                INSERT INTO events (
                    claim_id, event_type, from_status, to_status, details, payload_json, created_at,
                    prev_event_hash, event_hash, hash_algo
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    claim_id,
                    event_type,
                    from_status,
                    to_status,
                    details,
                    Jsonb(payload) if payload is not None else None,
                    created_at,
                    prev_event_hash,
                    event_hash,
                    EVENT_HASH_ALGO,
                ),
            )
            inserted = cur.fetchone()
        if inserted is None:
            raise RuntimeError("Failed to insert event row.")
        return int(inserted["id"])

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
        tenant_id: str | None = None,
    ) -> Claim:
        if not citations:
            raise ValueError("At least one citation is required.")
        normalized_idempotency_key = (idempotency_key or "").strip() or None
        normalized_tenant_id = (tenant_id or "").strip() or None
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO claims (
                        text, idempotency_key, normalized_text, claim_type, subject, predicate, object_value,
                        scope, volatility, status, confidence, pinned, supersedes_claim_id,
                        replaced_by_claim_id, created_at, updated_at, last_validated_at, archived_at,
                        tenant_id
                    ) VALUES (
                        %s, %s, NULL, %s, %s, %s, %s, %s, %s, 'candidate', %s, FALSE, NULL, NULL, %s, %s, NULL, NULL,
                        %s
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
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
                    normalized_tenant_id,
                ),
            )
            claim_row = cur.fetchone()
            if claim_row is None:
                if normalized_idempotency_key is None:
                    raise RuntimeError("Failed to create claim.")
                cur.execute(
                    "SELECT id FROM claims WHERE idempotency_key = %s",
                    (normalized_idempotency_key,),
                )
                existing_row = cur.fetchone()
                if existing_row is None:
                    raise RuntimeError("Idempotency key matched missing claim.")
                claim_id = int(existing_row["id"])
                claim = self.get_claim(claim_id)
                if claim is None:
                    raise RuntimeError("Idempotency key matched missing claim.")
                return claim
            claim_id = int(claim_row["id"])

            # Assign a human-readable ID.
            try:
                human_id = self._allocate_human_id(cur, subject, text, claim_id)
                cur.execute(
                    "UPDATE claims SET human_id = %s WHERE id = %s",
                    (human_id, claim_id),
                )
            except Exception:
                # Column may not exist in legacy schemas; skip gracefully.
                pass

            for cite in citations:
                cur.execute(
                    """
                        INSERT INTO citations (claim_id, source, locator, excerpt, created_at)
                        VALUES (%s, %s, %s, %s, %s)
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
                payload=ingest_payload,
                created_at=now,
            )

        claim = self.get_claim(claim_id)
        if claim is None:
            raise RuntimeError("Failed to load claim after insert.")
        return claim

    def get_claim(self, claim_id: int, include_citations: bool = True) -> Claim | None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM claims WHERE id = %s", (claim_id,))
            row = cur.fetchone()
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
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM claims WHERE idempotency_key = %s",
                (normalized_idempotency_key,),
            )
            row = cur.fetchone()
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
        tenant_id: str | None = None,
    ) -> list[Claim]:
        clauses: list[str] = []
        params: list[object] = []

        if tenant_id is not None:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)

        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        elif status_in:
            placeholders = ",".join("%s" for _ in status_in)
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_in)

        if not include_archived and status != "archived":
            clauses.append("status <> 'archived'")

        if text_query:
            clauses.append("(LOWER(text) LIKE %s OR LOWER(COALESCE(normalized_text, '')) LIKE %s)")
            needle = f"%{text_query.lower()}%"
            params.extend([needle, needle])

        if scope_allowlist:
            normalized_scopes = [scope.strip() for scope in scope_allowlist if scope and scope.strip()]
            if normalized_scopes:
                placeholders = ",".join("%s" for _ in normalized_scopes)
                clauses.append(f"scope IN ({placeholders})")
                params.extend(normalized_scopes)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT * FROM claims
            {where_sql}
            ORDER BY pinned DESC, confidence DESC, updated_at DESC, id DESC
            LIMIT %s
        """
        params.append(limit)

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        claims = [self._row_to_claim(row) for row in rows]
        if include_citations:
            for claim in claims:
                claim.citations = self.list_citations(claim.id)
        return claims

    def list_citations(self, claim_id: int) -> list[Citation]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM citations WHERE claim_id = %s ORDER BY id ASC",
                (claim_id,),
            )
            rows = cur.fetchall()
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
            clauses.append("claim_id = %s")
            params.append(claim_id)
        if event_type is not None:
            clauses.append("event_type = %s")
            params.append(event_type)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM events {where_sql} ORDER BY created_at DESC, id DESC LIMIT %s"
        params.append(limit)

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_event(row) for row in rows]

    def count_citations(self, claim_id: int) -> int:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM citations WHERE claim_id = %s", (claim_id,))
            row = cur.fetchone()
        return int(row["c"]) if row is not None else 0

    def set_normalized_text(self, claim_id: int, normalized_text: str) -> None:
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE claims SET normalized_text = %s, updated_at = %s WHERE id = %s",
                (normalized_text, now, claim_id),
            )

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

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM claims WHERE id = %s", (claim_id,))
            status_row = cur.fetchone()
            if status_row is None:
                raise ValueError(f"Claim {claim_id} does not exist.")
            current_status = str(status_row["status"]) if status_row["status"] is not None else None

            claim_rows = 0
            citation_rows = 0

            if redact_claim:
                cur.execute(
                    """
                        UPDATE claims
                        SET text = %s,
                            normalized_text = NULL,
                            subject = %s,
                            predicate = %s,
                            object_value = %s,
                            updated_at = %s
                        WHERE id = %s
                        """,
                    (claim_text, subject_value, predicate_value, object_value, now, claim_id),
                )
                claim_rows = int(cur.rowcount)

            if redact_citations:
                cur.execute(
                    """
                        UPDATE citations
                        SET source = %s, locator = %s, excerpt = %s
                        WHERE claim_id = %s
                        """,
                    (citation_source, citation_locator, citation_excerpt, claim_id),
                )
                citation_rows = int(cur.rowcount)
                if not redact_claim:
                    cur.execute(
                        "UPDATE claims SET updated_at = %s WHERE id = %s",
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
                payload=validated_payload,
                created_at=now,
            )

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
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    UPDATE claims
                    SET claim_type = COALESCE(claim_type, %s),
                        subject = COALESCE(subject, %s),
                        predicate = COALESCE(predicate, %s),
                        object_value = COALESCE(object_value, %s),
                        updated_at = %s
                    WHERE id = %s
                    """,
                (claim_type, subject, predicate, object_value, now, claim_id),
            )

    def set_confidence(self, claim_id: int, confidence: float, details: str | None = None) -> None:
        bounded = max(0.0, min(1.0, confidence))
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE claims SET confidence = %s, updated_at = %s WHERE id = %s",
                (bounded, now, claim_id),
            )
            if details:
                cur.execute("SELECT status FROM claims WHERE id = %s", (claim_id,))
                status_row = cur.fetchone()
                current_status = str(status_row["status"]) if status_row else None
                self._insert_event_row(
                    conn,
                    claim_id=claim_id,
                    event_type="confidence",
                    from_status=current_status,
                    to_status=current_status,
                    details=details,
                    payload=None,
                    created_at=now,
                )

    def set_pinned(self, claim_id: int, pinned: bool, reason: str) -> None:
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE claims SET pinned = %s, updated_at = %s WHERE id = %s",
                (pinned, now, claim_id),
            )
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type="pin" if pinned else "unpin",
                from_status=None,
                to_status=None,
                details=reason,
                payload=None,
                created_at=now,
            )

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

        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE claims
                SET status = %s, updated_at = %s, last_validated_at = %s, archived_at = %s, replaced_by_claim_id = %s
                WHERE id = %s
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
                payload={"replaced_by_claim_id": replaced_by_claim_id} if replaced_by_claim_id else None,
                created_at=now,
            )

        updated = self.get_claim(claim.id)
        if updated is None:
            raise RuntimeError("Failed to load claim after transition.")
        return updated

    def set_supersedes(self, claim_id: int, supersedes_claim_id: int) -> None:
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    UPDATE claims
                    SET supersedes_claim_id = %s, updated_at = %s
                    WHERE id = %s
                    """,
                (supersedes_claim_id, now, claim_id),
            )

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
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT * FROM claims
                    WHERE status = 'confirmed'
                      AND pinned = FALSE
                    ORDER BY updated_at ASC, id ASC
                    LIMIT %s
                    """,
                (limit,),
            )
            rows = cur.fetchall()
        return [self._row_to_claim(row) for row in rows]

    def find_for_compaction(self, retain_days: int, limit: int = 500) -> list[Claim]:
        cutoff = utc_now() - timedelta(days=retain_days)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT * FROM claims
                    WHERE status IN ('stale', 'superseded', 'conflicted')
                      AND pinned = FALSE
                      AND updated_at < %s
                    ORDER BY updated_at ASC, id ASC
                    LIMIT %s
                    """,
                (cutoff, limit),
            )
            rows = cur.fetchall()
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

        clauses = ["status = 'confirmed'", "subject = %s", "predicate = %s", "scope = %s"]
        params: list[object] = [subject, predicate, scope or "project"]
        if exclude_claim_id is not None:
            clauses.append("id <> %s")
            params.append(exclude_claim_id)

        sql = f"""
            SELECT * FROM claims
            WHERE {' AND '.join(clauses)}
            ORDER BY confidence DESC, updated_at DESC
        """
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_claim(row) for row in rows]

    def delete_old_events(self, retain_days: int) -> int:
        # Events are append-only by contract; retention trim is a no-op.
        return 0

    def reconcile_integrity(self, *, fix: bool = False, limit: int = 500) -> dict[str, object]:
        report: dict[str, object] = {
            "checked_at": utc_now().isoformat(),
            "fix_mode": bool(fix),
            "issues": {},
            "actions": [],
        }
        with self.connect() as conn:
            self._ensure_event_integrity_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT e.id
                    FROM events e
                    LEFT JOIN claims c ON c.id = e.claim_id
                    WHERE e.claim_id IS NOT NULL AND c.id IS NULL
                    ORDER BY e.id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                orphan_events = [int(row["id"]) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT ci.id
                    FROM citations ci
                    LEFT JOIN claims c ON c.id = ci.claim_id
                    WHERE c.id IS NULL
                    ORDER BY ci.id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                orphan_citations = [int(row["id"]) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT id
                    FROM claims
                    WHERE status = 'superseded' AND replaced_by_claim_id IS NULL
                    ORDER BY id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                superseded_without_replacement = [int(row["id"]) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT c.id
                    FROM claims c
                    LEFT JOIN claims n ON n.id = c.replaced_by_claim_id
                    WHERE c.replaced_by_claim_id IS NOT NULL AND n.id IS NULL
                    ORDER BY c.id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                dangling_replaced_by = [int(row["id"]) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT c.id
                    FROM claims c
                    LEFT JOIN claims p ON p.id = c.supersedes_claim_id
                    WHERE c.supersedes_claim_id IS NOT NULL AND p.id IS NULL
                    ORDER BY c.id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                dangling_supersedes = [int(row["id"]) for row in cur.fetchall()]

                transition_placeholders = ",".join("%s" for _ in STATUS_TRANSITION_EVENT_TYPES)
                cur.execute(
                    f"""
                    SELECT id, event_type, from_status, to_status
                    FROM events
                    WHERE event_type IN ({transition_placeholders})
                    ORDER BY id ASC
                    """,
                    list(STATUS_TRANSITION_EVENT_TYPES),
                )
                transition_rows = cur.fetchall()
                transition_issues: list[dict[str, object]] = []
                from memorymaster.lifecycle import ALLOWED_TRANSITIONS

                for row in transition_rows:
                    from_status = self._as_text(row["from_status"])
                    to_status = self._as_text(row["to_status"])
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
                    if to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
                        transition_issues.append(
                            {
                                "event_id": int(row["id"]),
                                "event_type": str(row["event_type"]),
                                "reason": "invalid_transition",
                                "from_status": from_status,
                                "to_status": to_status,
                            }
                        )

                cur.execute("SELECT id, prev_event_hash, event_hash, hash_algo FROM events ORDER BY id ASC")
                chain_rows = cur.fetchall()
                chain_issues: list[dict[str, object]] = []
                expected_prev: str | None = None
                for row in chain_rows:
                    row_prev = self._as_text(row["prev_event_hash"])
                    row_hash = self._as_text(row["event_hash"])
                    row_algo = self._as_text(row["hash_algo"])
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
                    "orphan_events": orphan_events,
                    "orphan_citations": orphan_citations,
                    "superseded_without_replacement": superseded_without_replacement,
                    "dangling_replaced_by": dangling_replaced_by,
                    "dangling_supersedes": dangling_supersedes,
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
                    if orphan_citations:
                        cur.execute("DELETE FROM citations WHERE id = ANY(%s)", (orphan_citations,))
                        actions.append({"action": "delete_orphan_citations", "rows": int(cur.rowcount)})
                    if orphan_events:
                        actions.append(
                            {
                                "action": "skip_delete_orphan_events_append_only",
                                "rows": 0,
                                "reason": "events table is append-only",
                            }
                        )
                    if dangling_replaced_by:
                        cur.execute(
                            "UPDATE claims SET replaced_by_claim_id = NULL WHERE id = ANY(%s)",
                            (dangling_replaced_by,),
                        )
                        actions.append({"action": "clear_dangling_replaced_by", "rows": int(cur.rowcount)})
                    if dangling_supersedes:
                        cur.execute(
                            "UPDATE claims SET supersedes_claim_id = NULL WHERE id = ANY(%s)",
                            (dangling_supersedes,),
                        )
                        actions.append({"action": "clear_dangling_supersedes", "rows": int(cur.rowcount)})
                    if chain_issues:
                        actions.append(
                            {
                                "action": "skip_rebuild_event_hash_chain_append_only",
                                "rows": 0,
                                "reason": "events table is append-only",
                            }
                        )
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
        with self.connect() as conn:
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type=validated_event_type,
                from_status=from_status,
                to_status=to_status,
                details=details,
                payload=validated_payload,
                created_at=now,
            )

    def _has_vector_table(self) -> bool:
        if self._vector_table_available is not None:
            return self._vector_table_available
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT EXISTS (
                      SELECT 1
                      FROM information_schema.tables
                      WHERE table_schema = 'public' AND table_name = 'claim_embeddings'
                    ) AS ok
                    """
            )
            row = cur.fetchone()
        self._vector_table_available = bool(row["ok"]) if row is not None else False
        return self._vector_table_available

    @staticmethod
    def _vector_literal(vec: list[float]) -> str:
        return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"

    def upsert_embeddings(self, claims: list[Claim], provider: EmbeddingProvider) -> int:
        if not claims:
            return 0
        if not self._has_vector_table():
            return 0
        now = utc_now()
        rows: list[tuple[object, ...]] = []
        for claim in claims:
            text = " ".join(
                part
                for part in [claim.text, claim.normalized_text or "", claim.subject or "", claim.object_value or ""]
                if part
            )
            emb = provider.embed(text)
            rows.append((claim.id, provider.model, self._vector_literal(emb), now))

        with self.connect() as conn, conn.cursor() as cur:
            cur.executemany(
                """
                    INSERT INTO claim_embeddings (claim_id, model, embedding, updated_at)
                    VALUES (%s, %s, %s::vector, %s)
                    ON CONFLICT (claim_id) DO UPDATE SET
                      model = EXCLUDED.model,
                      embedding = EXCLUDED.embedding,
                      updated_at = EXCLUDED.updated_at
                    """,
                rows,
            )
        return len(rows)

    def vector_scores(
        self,
        query_text: str,
        claims: list[Claim],
        provider: EmbeddingProvider,
    ) -> dict[int, float]:
        if not claims:
            return {}
        if not self._has_vector_table():
            return self._vector_scores_fallback(query_text, claims, provider)

        self.upsert_embeddings(claims, provider)
        query_vec = self._vector_literal(provider.embed(query_text))
        ids = [c.id for c in claims]
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT claim_id, 1 - (embedding <=> %s::vector) AS sim
                    FROM claim_embeddings
                    WHERE claim_id = ANY(%s)
                    """,
                (query_vec, ids),
            )
            rows = cur.fetchall()
        out: dict[int, float] = {}
        for row in rows:
            sim = float(row["sim"])
            out[int(row["claim_id"])] = max(0.0, min(1.0, (sim + 1.0) / 2.0))
        return out

    def _vector_scores_fallback(
        self,
        query_text: str,
        claims: list[Claim],
        provider: EmbeddingProvider,
    ) -> dict[int, float]:
        query_vec = provider.embed(query_text)
        out: dict[int, float] = {}
        for claim in claims:
            text = " ".join(
                part
                for part in [claim.text, claim.normalized_text or "", claim.subject or "", claim.object_value or ""]
                if part
            )
            emb = provider.embed(text)
            sim = cosine_similarity(query_vec, emb)
            out[claim.id] = max(0.0, min(1.0, (sim + 1.0) / 2.0))
        return out

    @staticmethod
    def _as_iso(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(microsecond=0).isoformat()
        return str(value)

    @staticmethod
    def _as_text(value: object) -> str | None:
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _handle_dollar_quote(
        sql: str,
        i: int,
        current: list[str],
        dollar_quote_tag: str | None,
    ) -> tuple[int, str | None]:
        """Handle dollar-quoted string transitions."""
        if dollar_quote_tag is not None:
            if sql.startswith(dollar_quote_tag, i):
                current.append(dollar_quote_tag)
                return i + len(dollar_quote_tag), None
            current.append(sql[i])
            return i + 1, dollar_quote_tag

        # Check if starting a new dollar quote
        if sql[i] == "$":
            j = i + 1
            while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < len(sql) and sql[j] == "$":
                tag = sql[i : j + 1]
                current.append(tag)
                return j + 1, tag

        return i, dollar_quote_tag

    @staticmethod
    def _handle_single_quote(
        sql: str,
        i: int,
        current: list[str],
        in_single_quote: bool,
    ) -> tuple[int, bool]:
        """Handle single-quoted string transitions."""
        if in_single_quote:
            current.append(sql[i])
            if sql[i] == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    current.append("'")
                    return i + 2, True
                return i + 1, False
            return i + 1, True

        if sql[i] == "'":
            current.append(sql[i])
            return i + 1, True

        return i, in_single_quote

    @staticmethod
    def _split_sql_statements(sql: str) -> list[str]:
        statements: list[str] = []
        current: list[str] = []
        in_single_quote = False
        dollar_quote_tag: str | None = None
        i = 0
        n = len(sql)

        while i < n:
            ch = sql[i]

            # Handle dollar-quoted strings
            new_i, new_tag = PostgresStore._handle_dollar_quote(sql, i, current, dollar_quote_tag)
            if new_i != i:
                i = new_i
                dollar_quote_tag = new_tag
                continue

            # Handle single-quoted strings
            new_i, new_in_quote = PostgresStore._handle_single_quote(sql, i, current, in_single_quote)
            if new_i != i or new_in_quote != in_single_quote:
                i = new_i
                in_single_quote = new_in_quote
                continue

            # Handle statement terminator
            if ch == ";":
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
                i += 1
                continue

            current.append(ch)
            i += 1

        tail = "".join(current).strip()
        if tail:
            statements.append(tail)
        return statements

    @classmethod
    def _row_to_claim(cls, row: dict[str, object]) -> Claim:
        return Claim(
            id=int(row["id"]),
            text=str(row["text"]),
            idempotency_key=cls._as_text(row.get("idempotency_key")),
            normalized_text=cls._as_text(row["normalized_text"]),
            claim_type=cls._as_text(row["claim_type"]),
            subject=cls._as_text(row["subject"]),
            predicate=cls._as_text(row["predicate"]),
            object_value=cls._as_text(row["object_value"]),
            scope=str(row["scope"]),
            volatility=str(row["volatility"]),
            status=str(row["status"]),
            confidence=float(row["confidence"]),
            pinned=bool(row["pinned"]),
            supersedes_claim_id=int(row["supersedes_claim_id"]) if row["supersedes_claim_id"] is not None else None,
            replaced_by_claim_id=int(row["replaced_by_claim_id"]) if row["replaced_by_claim_id"] is not None else None,
            created_at=cls._as_iso(row["created_at"]) or "",
            updated_at=cls._as_iso(row["updated_at"]) or "",
            last_validated_at=cls._as_iso(row["last_validated_at"]),
            archived_at=cls._as_iso(row["archived_at"]),
            human_id=cls._as_text(row.get("human_id")),
            tenant_id=cls._as_text(row.get("tenant_id")),
            wiki_article=cls._as_text(row.get("wiki_article")),
        )

    @classmethod
    def _row_to_citation(cls, row: dict[str, object]) -> Citation:
        return Citation(
            id=int(row["id"]),
            claim_id=int(row["claim_id"]),
            source=str(row["source"]),
            locator=cls._as_text(row["locator"]),
            excerpt=cls._as_text(row["excerpt"]),
            created_at=cls._as_iso(row["created_at"]) or "",
        )

    @classmethod
    def _row_to_event(cls, row: dict[str, object]) -> Event:
        payload_value = row["payload_json"]
        if payload_value is None:
            payload_json = None
        elif isinstance(payload_value, str):
            payload_json = payload_value
        else:
            payload_json = json.dumps(payload_value)

        return Event(
            id=int(row["id"]),
            claim_id=int(row["claim_id"]) if row["claim_id"] is not None else None,
            event_type=str(row["event_type"]),
            from_status=cls._as_text(row["from_status"]),
            to_status=cls._as_text(row["to_status"]),
            details=cls._as_text(row["details"]),
            payload_json=payload_json,
            created_at=cls._as_iso(row["created_at"]) or "",
        )

    @classmethod
    def _row_to_claim_link(cls, row: dict[str, object]) -> ClaimLink:
        return ClaimLink(
            id=int(row["id"]),
            source_id=int(row["source_id"]),
            target_id=int(row["target_id"]),
            link_type=str(row["link_type"]),
            created_at=cls._as_iso(row["created_at"]) or "",
        )

    def _ensure_claim_links_schema(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS claim_links (
                    id BIGSERIAL PRIMARY KEY,
                    source_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
                    target_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
                    link_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    CHECK (source_id <> target_id),
                    CHECK (link_type IN ('relates_to', 'supersedes', 'derived_from', 'contradicts', 'supports'))
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_links_unique ON claim_links(source_id, target_id, link_type)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_claim_links_source ON claim_links(source_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_claim_links_target ON claim_links(target_id)"
            )

    def _ensure_human_id_schema(self, conn) -> None:
        """Add human_id column if missing and backfill existing claims."""
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE claims ADD COLUMN IF NOT EXISTS human_id TEXT")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_human_id ON claims(human_id)"
            )
        self._backfill_human_ids(conn)

    def _backfill_human_ids(self, conn) -> int:
        """Assign human_id to all claims that lack one."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, subject, text FROM claims WHERE human_id IS NULL ORDER BY id ASC"
            )
            rows = cur.fetchall()
            if not rows:
                return 0
            updated = 0
            for row in rows:
                claim_id = int(row["id"])
                subject = self._as_text(row["subject"])
                text = str(row["text"])
                human_id = self._allocate_human_id(cur, subject, text, claim_id)
                cur.execute(
                    "UPDATE claims SET human_id = %s WHERE id = %s",
                    (human_id, claim_id),
                )
                updated += 1
            return updated

    @staticmethod
    def _allocate_human_id(cur, subject: str | None, text: str, claim_id: int) -> str:
        """Build a unique human_id, checking for derived_from parent links."""
        cur.execute(
            """
            SELECT c.human_id
            FROM claim_links cl
            JOIN claims c ON c.id = cl.target_id
            WHERE cl.source_id = %s
              AND cl.link_type = 'derived_from'
              AND c.human_id IS NOT NULL
            LIMIT 1
            """,
            (claim_id,),
        )
        parent_row = cur.fetchone()

        if parent_row and parent_row["human_id"]:
            parent_hid = str(parent_row["human_id"])
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM claims WHERE human_id LIKE %s AND human_id != %s",
                (parent_hid + ".%", parent_hid),
            )
            child_count = cur.fetchone()
            next_child = (int(child_count["cnt"]) if child_count else 0) + 1
            candidate = f"{parent_hid}.{next_child}"
        else:
            candidate = generate_top_level_human_id(subject, text)

        final = candidate
        suffix = 1
        while True:
            cur.execute("SELECT 1 FROM claims WHERE human_id = %s", (final,))
            existing = cur.fetchone()
            if existing is None:
                return final
            suffix += 1
            final = f"{candidate}~{suffix}"

    def _ensure_tenant_id_schema(self, conn) -> None:
        """Add tenant_id column if missing, with an index for tenant isolation."""
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE claims ADD COLUMN IF NOT EXISTS tenant_id TEXT")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_claims_tenant_id ON claims(tenant_id)"
            )

    def _ensure_binding_schema(self, conn) -> None:
        """Add wiki_article column for claim↔wiki bidirectional binding (v3.4)."""
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE claims ADD COLUMN IF NOT EXISTS wiki_article TEXT")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_claims_wiki_article ON claims(wiki_article)"
            )

    def get_claim_by_human_id(self, human_id: str, include_citations: bool = True) -> Claim | None:
        """Look up a claim by its human-readable ID (e.g. ``mm-a3f8``)."""
        normalized = human_id.strip()
        if not normalized:
            return None
        with self.connect() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT * FROM claims WHERE human_id = %s",
                    (normalized,),
                )
                row = cur.fetchone()
            except Exception:
                # Column may not exist yet.
                return None
        if row is None:
            return None
        claim = self._row_to_claim(row)
        if include_citations:
            claim.citations = self.list_citations(claim.id)
        return claim

    def resolve_claim_id(self, identifier: str | int) -> int:
        """Resolve a numeric ID or human_id string to a numeric claim ID."""
        if isinstance(identifier, int):
            return identifier
        raw = str(identifier).strip()
        try:
            return int(raw)
        except ValueError:
            pass
        claim = self.get_claim_by_human_id(raw, include_citations=False)
        if claim is not None:
            return claim.id
        raise ValueError(f"No claim found for identifier '{raw}'.")

    def add_claim_link(self, source_id: int, target_id: int, link_type: str) -> ClaimLink:
        if link_type not in CLAIM_LINK_TYPES:
            allowed = ", ".join(CLAIM_LINK_TYPES)
            raise ValueError(f"Invalid link_type '{link_type}'. Allowed: {allowed}.")
        if source_id == target_id:
            raise ValueError("source_id and target_id must be different.")
        now = utc_now()
        with self.connect() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    """
                        INSERT INTO claim_links (source_id, target_id, link_type, created_at)
                        VALUES (%s, %s, %s, %s)
                        RETURNING id
                        """,
                    (source_id, target_id, link_type, now),
                )
                row = cur.fetchone()
            except Exception as exc:
                msg = str(exc).lower()
                if "unique" in msg or "duplicate key" in msg or "already exists" in msg:
                    raise ValueError(
                        f"Link already exists: {source_id} -> {target_id} ({link_type})."
                    ) from exc
                if "foreign key" in msg or "violates foreign key" in msg or "is not present" in msg:
                    raise ValueError(
                        f"One or both claim ids do not exist: {source_id}, {target_id}."
                    ) from exc
                if "check" in msg and "source_id" in msg:
                    raise ValueError("source_id and target_id must be different.") from exc
                raise
            if row is None:
                raise RuntimeError("Failed to insert claim link.")
            return ClaimLink(
                id=int(row["id"]),
                source_id=source_id,
                target_id=target_id,
                link_type=link_type,
                created_at=self._as_iso(now) or "",
            )

    def remove_claim_link(self, source_id: int, target_id: int, link_type: str | None = None) -> int:
        with self.connect() as conn, conn.cursor() as cur:
            if link_type is not None:
                cur.execute(
                    "DELETE FROM claim_links WHERE source_id = %s AND target_id = %s AND link_type = %s",
                    (source_id, target_id, link_type),
                )
            else:
                cur.execute(
                    "DELETE FROM claim_links WHERE source_id = %s AND target_id = %s",
                    (source_id, target_id),
                )
            return cur.rowcount

    def get_derived_from_target_ids(self, candidate_ids: list[int]) -> set[int]:
        """Return the subset of *candidate_ids* that are targets of a ``derived_from`` link."""
        if not candidate_ids:
            return set()
        with self.connect() as conn, conn.cursor() as cur:
            placeholders = ",".join("%s" for _ in candidate_ids)
            cur.execute(
                f"""
                    SELECT DISTINCT target_id FROM claim_links
                    WHERE link_type = 'derived_from'
                      AND target_id IN ({placeholders})
                    """,
                candidate_ids,
            )
            rows = cur.fetchall()
        return {row[0] if isinstance(row, (tuple, list)) else row["target_id"] for row in rows}

    def get_claim_links(self, claim_id: int) -> list[ClaimLink]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT * FROM claim_links
                    WHERE source_id = %s OR target_id = %s
                    ORDER BY created_at ASC
                    """,
                (claim_id, claim_id),
            )
            rows = cur.fetchall()
        return [self._row_to_claim_link(row) for row in rows]

    def get_linked_claims(self, claim_id: int, link_type: str | None = None) -> list[ClaimLink]:
        with self.connect() as conn, conn.cursor() as cur:
            if link_type is not None:
                cur.execute(
                    """
                        SELECT * FROM claim_links
                        WHERE (source_id = %s OR target_id = %s) AND link_type = %s
                        ORDER BY created_at ASC
                        """,
                    (claim_id, claim_id, link_type),
                )
            else:
                cur.execute(
                    """
                        SELECT * FROM claim_links
                        WHERE source_id = %s OR target_id = %s
                        ORDER BY created_at ASC
                        """,
                    (claim_id, claim_id),
                )
            rows = cur.fetchall()
        return [self._row_to_claim_link(row) for row in rows]
