from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.retry import connect_with_retry
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
from memorymaster.schema import load_schema_sql

logger = logging.getLogger(__name__)

HUMAN_ID_PREFIX = "mm"


def generate_human_id_hash(text: str) -> str:
    """Generate a 4-hex-char hash from text for human-readable IDs."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:4]


def generate_top_level_human_id(subject: str | None, text: str) -> str:
    """Generate a top-level human_id like ``mm-a3f8``."""
    seed = (subject or text).strip()
    return f"{HUMAN_ID_PREFIX}-{generate_human_id_hash(seed)}"


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


class ConcurrentModificationError(RuntimeError):
    """Raised when an optimistic-lock check fails during a status transition."""


class SQLiteStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    def connect(self) -> sqlite3.Connection:
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            return conn

        return connect_with_retry(_open)

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(load_schema_sql())
            conn.commit()
            self._ensure_claim_idempotency_schema(conn)
            self._ensure_confirmed_tuple_uniqueness_schema(conn)
            self._ensure_event_integrity_schema(conn)
            self._ensure_fts5_schema(conn)
            self._ensure_claim_links_schema(conn)
            self._ensure_human_id_schema(conn)
            self._ensure_tenant_id_schema(conn)
            self._ensure_temporal_columns(conn)
            self._ensure_tiering_columns(conn)
            self._ensure_agent_columns(conn)
            self._ensure_version_column(conn)
            self._ensure_embeddings_schema(conn)
            conn.commit()

    @staticmethod
    def _ensure_version_column(conn: sqlite3.Connection) -> None:
        """Add ``version`` column to claims (optimistic-locking counter)."""
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    @staticmethod
    def _ensure_embeddings_schema(conn: sqlite3.Connection) -> None:
        """Ensure claim_embeddings table exists for vector search."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS claim_embeddings (
                claim_id INTEGER PRIMARY KEY,
                model TEXT NOT NULL,
                embedding_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embeddings_updated_at ON claim_embeddings(updated_at)"
        )

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
    def _fts5_available(conn: sqlite3.Connection) -> bool:
        """Check if the FTS5 extension is available in the current SQLite build."""
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
            conn.execute("DROP TABLE IF EXISTS _fts5_probe")
            return True
        except sqlite3.OperationalError:
            return False

    @staticmethod
    def _ensure_fts5_schema(conn: sqlite3.Connection) -> None:
        """Create the FTS5 virtual table, sync triggers, and backfill from existing claims."""
        if not SQLiteStore._fts5_available(conn):
            return

        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(
                text,
                normalized_text,
                subject,
                predicate,
                object_value,
                content='claims',
                content_rowid='id'
            )
            """
        )

        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS trg_claims_fts_insert
            AFTER INSERT ON claims
            BEGIN
                INSERT INTO claims_fts(rowid, text, normalized_text, subject, predicate, object_value)
                VALUES (
                    NEW.id,
                    NEW.text,
                    COALESCE(NEW.normalized_text, ''),
                    COALESCE(NEW.subject, ''),
                    COALESCE(NEW.predicate, ''),
                    COALESCE(NEW.object_value, '')
                );
            END;

            CREATE TRIGGER IF NOT EXISTS trg_claims_fts_update
            AFTER UPDATE OF text, normalized_text, subject, predicate, object_value ON claims
            BEGIN
                INSERT INTO claims_fts(claims_fts, rowid, text, normalized_text, subject, predicate, object_value)
                VALUES (
                    'delete',
                    OLD.id,
                    OLD.text,
                    COALESCE(OLD.normalized_text, ''),
                    COALESCE(OLD.subject, ''),
                    COALESCE(OLD.predicate, ''),
                    COALESCE(OLD.object_value, '')
                );
                INSERT INTO claims_fts(rowid, text, normalized_text, subject, predicate, object_value)
                VALUES (
                    NEW.id,
                    NEW.text,
                    COALESCE(NEW.normalized_text, ''),
                    COALESCE(NEW.subject, ''),
                    COALESCE(NEW.predicate, ''),
                    COALESCE(NEW.object_value, '')
                );
            END;

            CREATE TRIGGER IF NOT EXISTS trg_claims_fts_delete
            AFTER DELETE ON claims
            BEGIN
                INSERT INTO claims_fts(claims_fts, rowid, text, normalized_text, subject, predicate, object_value)
                VALUES (
                    'delete',
                    OLD.id,
                    OLD.text,
                    COALESCE(OLD.normalized_text, ''),
                    COALESCE(OLD.subject, ''),
                    COALESCE(OLD.predicate, ''),
                    COALESCE(OLD.object_value, '')
                );
            END;
            """
        )

        # Backfill: rebuild FTS index from existing claims data.
        # The 'rebuild' command re-reads all rows from the content table.
        conn.execute("INSERT INTO claims_fts(claims_fts) VALUES ('rebuild')")

    @staticmethod
    def _ensure_claim_links_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS claim_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                link_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (source_id) REFERENCES claims(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES claims(id) ON DELETE CASCADE,
                CHECK (source_id <> target_id),
                CHECK (link_type IN ('relates_to', 'supersedes', 'derived_from', 'contradicts', 'supports'))
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_links_unique ON claim_links(source_id, target_id, link_type)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_links_source ON claim_links(source_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_links_target ON claim_links(target_id)"
        )

    @staticmethod
    def _ensure_human_id_schema(conn: sqlite3.Connection) -> None:
        """Add human_id column if missing and backfill existing claims."""
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN human_id TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_human_id ON claims(human_id)"
        )
        SQLiteStore._backfill_human_ids(conn)

    @staticmethod
    def _backfill_human_ids(conn: sqlite3.Connection) -> int:
        """Assign human_id to all claims that lack one."""
        rows = conn.execute(
            "SELECT id, subject, text FROM claims WHERE human_id IS NULL ORDER BY id ASC"
        ).fetchall()
        if not rows:
            return 0
        updated = 0
        for row in rows:
            claim_id = int(row["id"])
            subject = row["subject"]
            text = str(row["text"])
            human_id = SQLiteStore._allocate_human_id(conn, subject, text, claim_id)
            conn.execute(
                "UPDATE claims SET human_id = ? WHERE id = ?",
                (human_id, claim_id),
            )
            updated += 1
        return updated

    @staticmethod
    def _allocate_human_id(
        conn: sqlite3.Connection,
        subject: str | None,
        text: str,
        claim_id: int,
    ) -> str:
        """Build a unique human_id, checking for derived_from parent links.

        If the claim has a ``derived_from`` link to a parent that already has a
        human_id, produce a child id (e.g. ``mm-a3f8.1``).  Otherwise produce a
        top-level id (e.g. ``mm-a3f8``).  Collisions are resolved by appending a
        numeric suffix.
        """
        parent_row = conn.execute(
            """
            SELECT c.human_id
            FROM claim_links cl
            JOIN claims c ON c.id = cl.target_id
            WHERE cl.source_id = ?
              AND cl.link_type = 'derived_from'
              AND c.human_id IS NOT NULL
            LIMIT 1
            """,
            (claim_id,),
        ).fetchone()

        if parent_row and parent_row["human_id"]:
            parent_hid = str(parent_row["human_id"])
            child_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM claims WHERE human_id LIKE ? AND human_id != ?",
                (parent_hid + ".%", parent_hid),
            ).fetchone()
            next_child = (int(child_count["cnt"]) if child_count else 0) + 1
            candidate = f"{parent_hid}.{next_child}"
        else:
            candidate = generate_top_level_human_id(subject, text)

        # Resolve collisions by appending a numeric suffix.
        final = candidate
        suffix = 1
        while True:
            existing = conn.execute(
                "SELECT 1 FROM claims WHERE human_id = ?", (final,)
            ).fetchone()
            if existing is None:
                return final
            suffix += 1
            final = f"{candidate}~{suffix}"

    @staticmethod
    def _ensure_tenant_id_schema(conn: sqlite3.Connection) -> None:
        """Add tenant_id column if missing, with an index for tenant isolation."""
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN tenant_id TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claims_tenant_id ON claims(tenant_id)"
        )

    @staticmethod
    def _ensure_temporal_columns(conn) -> None:
        """Add bi-temporal columns if missing (backward compat for old DBs)."""
        for col in ("event_time", "valid_from", "valid_until"):
            try:
                conn.execute(f"ALTER TABLE claims ADD COLUMN {col} TEXT")
            except Exception as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claims_valid_from ON claims(valid_from)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claims_valid_until ON claims(valid_until)"
        )

    @staticmethod
    def _ensure_agent_columns(conn: sqlite3.Connection) -> None:
        """Add source_agent and visibility columns if missing."""
        for stmt in (
            "ALTER TABLE claims ADD COLUMN source_agent TEXT",
            "ALTER TABLE claims ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_source_agent ON claims(source_agent)")

    @staticmethod
    def _ensure_tiering_columns(conn: sqlite3.Connection) -> None:
        """Add tier, access_count, last_accessed columns if missing (migration for old DBs)."""
        for stmt in (
            "ALTER TABLE claims ADD COLUMN tier TEXT NOT NULL DEFAULT 'working'",
            "ALTER TABLE claims ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE claims ADD COLUMN last_accessed TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_tier ON claims(tier)")

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
            keys = row.keys()
            row_hash = row["event_hash"] if "event_hash" in keys else None
            row_algo = row["hash_algo"] if "hash_algo" in keys else None
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
        try:
            prev_row = conn.execute(
                "SELECT event_hash FROM events WHERE event_hash IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            prev_row = None
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
            exc_str = str(exc).lower()
            if "no column named" not in exc_str and "no such table" not in exc_str:
                raise
            if "no such table" in exc_str:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        claim_id INTEGER,
                        event_type TEXT NOT NULL,
                        from_status TEXT, to_status TEXT,
                        details TEXT, payload_json TEXT,
                        created_at TEXT NOT NULL,
                        prev_event_hash TEXT, event_hash TEXT, hash_algo TEXT
                    )
                """)
            cur = conn.execute(
                """
                INSERT INTO events (claim_id, event_type, from_status, to_status, details, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (claim_id, event_type, from_status, to_status, details, payload_json, created_at),
            )
            return int(cur.lastrowid)

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
        event_time: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        source_agent: str | None = None,
        visibility: str = "public",
    ) -> Claim:
        if not citations:
            raise ValueError("At least one citation is required.")
        normalized_idempotency_key = (idempotency_key or "").strip() or None
        normalized_tenant_id = (tenant_id or "").strip() or None
        now = utc_now()
        with self.connect() as conn:
            existing = self._check_idempotency(conn, idempotency_key)
            if existing is not None:
                return existing

            try:
                cur = conn.execute(
                    """
                    INSERT INTO claims (
                        text, idempotency_key, normalized_text, claim_type, subject, predicate, object_value,
                        scope, volatility, status, confidence, pinned, supersedes_claim_id,
                        replaced_by_claim_id, created_at, updated_at, last_validated_at, archived_at,
                        tenant_id, event_time, valid_from, valid_until, source_agent, visibility
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'candidate', ?, 0, NULL, NULL, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
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
                        event_time or None,
                        valid_from or None,
                        valid_until or None,
                        source_agent or None,
                        visibility or "public",
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
                    raise RuntimeError("Idempotency key matched missing claim.") from None
                return existing

            claim_id = int(cur.lastrowid)
            # Assign a human-readable ID.
            try:
                human_id = self._allocate_human_id(conn, subject, text, claim_id)
                conn.execute(
                    "UPDATE claims SET human_id = ? WHERE id = ?",
                    (human_id, claim_id),
                )
            except sqlite3.OperationalError:
                # Column may not exist in legacy schemas; skip gracefully.
                pass
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

            rows = conn.execute(sql, params).fetchall()

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

    def set_normalized_text(self, claim_id: int, normalized_text: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE claims SET normalized_text = ?, updated_at = ? WHERE id = ?",
                (normalized_text, now, claim_id),
            )
            conn.commit()

    def set_normalized_texts_batch(self, updates: dict[int, str]) -> None:
        """Batch update normalized_text for multiple claims in a single transaction.

        Args:
            updates: dict mapping claim_id -> normalized_text
        """
        if not updates:
            return
        now = utc_now()
        with self.connect() as conn:
            for claim_id, normalized_text in updates.items():
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
            # Read status BEFORE update to avoid race condition in event audit trail
            current_status = None
            if details:
                status_row = conn.execute("SELECT status FROM claims WHERE id = ?", (claim_id,)).fetchone()
                current_status = str(status_row["status"]) if status_row else None
            conn.execute(
                "UPDATE claims SET confidence = ?, updated_at = ? WHERE id = ?",
                (bounded, now, claim_id),
            )
            if details:
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
            cur = conn.execute(
                """
                UPDATE claims
                SET status = ?, updated_at = ?, last_validated_at = ?, archived_at = ?,
                    replaced_by_claim_id = ?, version = version + 1
                WHERE id = ? AND version = ?
                """,
                (to_status, now, last_validated_at, archived_at, next_replaced_by, claim.id, claim.version),
            )
            if cur.rowcount == 0:
                raise ConcurrentModificationError(
                    f"Claim {claim.id} was modified by another writer (version mismatch). Reload and retry."
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

        try:
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
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                logger.warning("claim_embeddings table missing, recreating: %s", exc)
                try:
                    # Create the table and ensure it's committed before retrying
                    with self.connect() as create_conn:
                        self._ensure_embeddings_schema(create_conn)
                        create_conn.commit()
                    # Retry the insert
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
                except Exception as retry_exc:
                    logger.error("Failed to recreate claim_embeddings: %s", retry_exc)
                    return 0
            else:
                raise
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
        keys = row.keys()
        idempotency_key = row["idempotency_key"] if "idempotency_key" in keys else None
        human_id = row["human_id"] if "human_id" in keys else None
        tenant_id = row["tenant_id"] if "tenant_id" in keys else None
        tier = row["tier"] if "tier" in keys else "working"
        access_count = int(row["access_count"]) if "access_count" in keys else 0
        last_accessed_val = row["last_accessed"] if "last_accessed" in keys else None
        version = int(row["version"]) if "version" in keys and row["version"] is not None else 1
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
        )

    def record_access(self, claim_id: int) -> None:
        """Increment access_count and set last_accessed for a claim."""
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE claims SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (now, claim_id),
            )
            conn.commit()

    def record_accesses_batch(self, claim_ids: list[int]) -> None:
        """Batch update access_count and last_accessed for multiple claims in a single transaction.

        Much faster than calling record_access() in a loop when there are many claims.
        """
        if not claim_ids:
            return
        now = utc_now()
        with self.connect() as conn:
            # Use a single UPDATE statement with IN clause
            placeholders = ",".join("?" * len(claim_ids))
            conn.execute(
                f"UPDATE claims SET access_count = access_count + 1, last_accessed = ? WHERE id IN ({placeholders})",
                [now] + claim_ids,
            )
            conn.commit()

    def recompute_tiers(self) -> dict[str, int]:
        """Recompute tier for all non-archived claims based on access_count and age.

        Rules:
          - access_count > 5 OR created less than 7 days ago -> core
          - access_count = 0 AND created more than 90 days ago -> peripheral
          - everything else -> working
        """
        now = datetime.now(timezone.utc)
        core_cutoff = (now - timedelta(days=7)).replace(microsecond=0).isoformat()
        peripheral_cutoff = (now - timedelta(days=90)).replace(microsecond=0).isoformat()

        counts = {"core": 0, "working": 0, "peripheral": 0}
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE claims SET tier = 'core' "
                "WHERE status != 'archived' AND tier != 'core' "
                "AND (access_count > 5 OR created_at > ?)",
                (core_cutoff,),
            )
            counts["core"] = cur.rowcount

            cur = conn.execute(
                "UPDATE claims SET tier = 'peripheral' "
                "WHERE status != 'archived' AND tier != 'peripheral' "
                "AND access_count = 0 AND created_at <= ?",
                (peripheral_cutoff,),
            )
            counts["peripheral"] = cur.rowcount

            cur = conn.execute(
                "UPDATE claims SET tier = 'working' "
                "WHERE status != 'archived' AND tier != 'working' "
                "AND NOT (access_count > 5 OR created_at > ?) "
                "AND NOT (access_count = 0 AND created_at <= ?)",
                (core_cutoff, peripheral_cutoff),
            )
            counts["working"] = cur.rowcount

            conn.commit()
        return counts

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

    def add_claim_link(self, source_id: int, target_id: int, link_type: str) -> ClaimLink:
        if link_type not in CLAIM_LINK_TYPES:
            allowed = ", ".join(CLAIM_LINK_TYPES)
            raise ValueError(f"Invalid link_type '{link_type}'. Allowed: {allowed}.")
        if source_id == target_id:
            raise ValueError("source_id and target_id must be different.")
        now = utc_now()
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO claim_links (source_id, target_id, link_type, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (source_id, target_id, link_type, now),
                )
            except sqlite3.IntegrityError as exc:
                msg = str(exc).lower()
                if "unique" in msg:
                    raise ValueError(
                        f"Link already exists: {source_id} -> {target_id} ({link_type})."
                    ) from exc
                if "foreign key" in msg:
                    raise ValueError(
                        f"One or both claim ids do not exist: {source_id}, {target_id}."
                    ) from exc
                raise
            conn.commit()
            return ClaimLink(
                id=int(cur.lastrowid),
                source_id=source_id,
                target_id=target_id,
                link_type=link_type,
                created_at=now,
            )

    def remove_claim_link(self, source_id: int, target_id: int, link_type: str | None = None) -> int:
        with self.connect() as conn:
            if link_type is not None:
                cur = conn.execute(
                    "DELETE FROM claim_links WHERE source_id = ? AND target_id = ? AND link_type = ?",
                    (source_id, target_id, link_type),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM claim_links WHERE source_id = ? AND target_id = ?",
                    (source_id, target_id),
                )
            conn.commit()
            return cur.rowcount

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
