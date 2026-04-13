"""Schema migrations and event-integrity helpers for _SchemaMixin.

This is a mixin class for memorymaster.storage._SchemaMixin. All methods
expect to be bound to a SQLiteStore instance and rely on `self.connect()`
and `self.db_path`. Do not instantiate directly.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3


logger = logging.getLogger(__name__)

from memorymaster._storage_shared import (
    EVENT_HASH_ALGO,
    SQLITE_CONFIRMED_TUPLE_GUARD_TRIGGERS,
    SQLITE_EVENTS_APPEND_ONLY_TRIGGERS,
    generate_top_level_human_id,
)


class _SchemaMixin:

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
        _SchemaMixin._drop_events_append_only_triggers(conn)
        _SchemaMixin._backfill_event_chain(conn)
        _SchemaMixin._ensure_events_append_only_triggers(conn)


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
        if not _SchemaMixin._fts5_available(conn):
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
        from memorymaster.models import CLAIM_LINK_TYPES

        # Build the CHECK constraint from the canonical CLAIM_LINK_TYPES tuple
        # so new types only need to be added in models.py.
        types_sql = ", ".join(f"'{t}'" for t in CLAIM_LINK_TYPES)
        check_clause = f"CHECK (link_type IN ({types_sql}))"

        # Check if table exists and whether it needs migration (old CHECK with only 5 types)
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='claim_links'"
        ).fetchone()

        if existing and existing[0]:
            # Table exists — check if it has the old 5-type CHECK
            if "'implements'" not in existing[0]:
                # Migrate: rename old → create new → copy → drop old
                conn.execute("ALTER TABLE claim_links RENAME TO _claim_links_old")
                conn.execute(f"""
                    CREATE TABLE claim_links (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_id INTEGER NOT NULL,
                        target_id INTEGER NOT NULL,
                        link_type TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (source_id) REFERENCES claims(id) ON DELETE CASCADE,
                        FOREIGN KEY (target_id) REFERENCES claims(id) ON DELETE CASCADE,
                        CHECK (source_id <> target_id),
                        {check_clause}
                    )
                """)
                conn.execute("""
                    INSERT INTO claim_links (id, source_id, target_id, link_type, created_at)
                    SELECT id, source_id, target_id, link_type, created_at FROM _claim_links_old
                """)
                conn.execute("DROP TABLE _claim_links_old")
        else:
            # Fresh creation
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS claim_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    link_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (source_id) REFERENCES claims(id) ON DELETE CASCADE,
                    FOREIGN KEY (target_id) REFERENCES claims(id) ON DELETE CASCADE,
                    CHECK (source_id <> target_id),
                    {check_clause}
                )
            """)

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
        _SchemaMixin._backfill_human_ids(conn)


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
            human_id = _SchemaMixin._allocate_human_id(conn, subject, text, claim_id)
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
    def _ensure_binding_columns(conn: sqlite3.Connection) -> None:
        """Add wiki_article column for claim↔wiki bidirectional binding (v3.4)."""
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN wiki_article TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claims_wiki_article ON claims(wiki_article)"
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
            _SchemaMixin._canonical_payload(payload_json),
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
            event_hash = _SchemaMixin._compute_event_hash(
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
        event_hash = _SchemaMixin._compute_event_hash(
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

