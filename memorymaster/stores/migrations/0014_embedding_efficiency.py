"""Persist embedding fingerprints and replayable Qdrant sync cursors."""

from __future__ import annotations

VERSION = 14
DESCRIPTION = "Embedding fingerprints and Qdrant sync cursor state"


def apply_sqlite(conn) -> None:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(claim_embeddings)").fetchall()
    }
    if columns and "content_hash" not in columns:
        conn.execute(
            "ALTER TABLE claim_embeddings "
            "ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qdrant_sync_state (
            stream_key TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT '',
            last_claim_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def apply_postgres(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE IF EXISTS claim_embeddings "
            "ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT ''"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS qdrant_sync_state (
                stream_key TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                last_claim_id BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        cur.execute("ALTER TABLE qdrant_sync_state ENABLE ROW LEVEL SECURITY")
        cur.execute("ALTER TABLE qdrant_sync_state FORCE ROW LEVEL SECURITY")
        cur.execute(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename = 'qdrant_sync_state'
                      AND policyname = 'memorymaster_qdrant_sync_tenant'
                ) THEN
                    CREATE POLICY memorymaster_qdrant_sync_tenant
                    ON qdrant_sync_state
                    USING (
                        tenant_id = current_setting('memorymaster.tenant_id', true)
                    )
                    WITH CHECK (
                        tenant_id = current_setting('memorymaster.tenant_id', true)
                    );
                END IF;
            END $$
            """
        )
    conn.commit()
