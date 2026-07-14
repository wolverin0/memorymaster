"""Indexes for the event query shapes used by governance and dashboards."""

from __future__ import annotations

VERSION = 15
DESCRIPTION = "Event query indexes for type, details, and time ordering"


def apply_sqlite(conn) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    if not exists:
        conn.commit()
        return
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_type_created_id "
        "ON events(event_type, created_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_type_details_created "
        "ON events(event_type, details, created_at DESC)"
    )
    conn.commit()


def apply_postgres(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            DO $$ BEGIN
                IF to_regclass('public.events') IS NOT NULL THEN
                    CREATE INDEX IF NOT EXISTS idx_events_type_created_id
                        ON events(event_type, created_at DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_events_type_details_created
                        ON events(event_type, details, created_at DESC);
                END IF;
            END $$
            """
        )
    conn.commit()
