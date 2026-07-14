"""Replay-safe worker leases for media retry rows."""

from __future__ import annotations

VERSION = 16
DESCRIPTION = "Add explicit owner and expiry to media retry worker leases"


def apply_sqlite(conn) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_retry_queue'"
    ).fetchone()
    if not exists:
        conn.commit()
        return
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(media_retry_queue)").fetchall()
    }
    if "lease_owner" not in columns:
        conn.execute("ALTER TABLE media_retry_queue ADD COLUMN lease_owner TEXT")
    if "lease_expires_at" not in columns:
        conn.execute("ALTER TABLE media_retry_queue ADD COLUMN lease_expires_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_retry_lease_expiry "
        "ON media_retry_queue(status, lease_expires_at)"
    )
    conn.commit()


def apply_postgres(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            DO $$ BEGIN
                IF to_regclass('public.media_retry_queue') IS NOT NULL THEN
                    ALTER TABLE media_retry_queue ADD COLUMN IF NOT EXISTS lease_owner TEXT;
                    ALTER TABLE media_retry_queue ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
                    CREATE INDEX IF NOT EXISTS idx_media_retry_lease_expiry
                        ON media_retry_queue(status, lease_expires_at);
                END IF;
            END $$
            """
        )
    conn.commit()
