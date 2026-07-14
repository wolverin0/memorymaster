"""Add tenant-partitioned integrity metadata to the append-only event ledger."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

VERSION = 10
DESCRIPTION = "Tenant ownership for the append-only event ledger"

# These algorithms are deliberately frozen in this checksummed migration.
# Runtime helpers have golden-vector tests against these implementations.
_EVENT_HASH_ALGO = "sha256-v1"
_TENANT_EVENT_HASH_ALGO = "sha256-tenant-v2"

_EVENT_ROWS_SQL = """
SELECT id, claim_id, event_type, from_status, to_status, details,
       payload_json, created_at, prev_event_hash, event_hash, hash_algo,
       tenant_id, tenant_prev_event_hash, tenant_event_hash, tenant_hash_algo
FROM events ORDER BY id ASC
""".strip()

_SQLITE_APPEND_TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER IF NOT EXISTS trg_events_append_only_update
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events table is append-only; UPDATE is not allowed');
    END
    """.strip(),
    """
    CREATE TRIGGER IF NOT EXISTS trg_events_append_only_delete
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events table is append-only; DELETE is not allowed');
    END
    """.strip(),
)

_POSTGRES_PREPARE_DDL = """
LOCK TABLE events IN ACCESS EXCLUSIVE MODE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS tenant_prev_event_hash TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS tenant_event_hash TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS tenant_hash_algo TEXT;
DROP TRIGGER IF EXISTS trg_events_append_only_update ON events;
DROP TRIGGER IF EXISTS trg_events_append_only_delete ON events;
CREATE INDEX IF NOT EXISTS idx_events_tenant_id ON events(tenant_id);
CREATE INDEX IF NOT EXISTS idx_events_tenant_hash
    ON events(tenant_id, tenant_event_hash);
CREATE INDEX IF NOT EXISTS idx_events_tenant_head
    ON events(tenant_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_events_tenant_algo_head
    ON events(tenant_id, hash_algo, id DESC);
""".strip()

_POSTGRES_FINALIZE_DDL = """
CREATE OR REPLACE FUNCTION memorymaster_events_append_only_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'events table is append-only; % is not allowed', TG_OP;
END;
$$;
CREATE TRIGGER trg_events_append_only_update
BEFORE UPDATE ON events
FOR EACH ROW EXECUTE FUNCTION memorymaster_events_append_only_guard();
CREATE TRIGGER trg_events_append_only_delete
BEFORE DELETE ON events
FOR EACH ROW EXECUTE FUNCTION memorymaster_events_append_only_guard();
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memorymaster_tenant_restrict ON events;
CREATE POLICY memorymaster_tenant_restrict ON events
AS RESTRICTIVE FOR ALL TO PUBLIC
USING (
    events.tenant_id = NULLIF(current_setting('memorymaster.tenant_id', true), '')
    AND (
      events.claim_id IS NULL
      OR EXISTS (
        SELECT 1 FROM claims AS mm_claim
        WHERE mm_claim.id = events.claim_id
          AND mm_claim.tenant_id = NULLIF(current_setting('memorymaster.tenant_id', true), '')
      )
    )
)
WITH CHECK (
    events.tenant_id = NULLIF(current_setting('memorymaster.tenant_id', true), '')
    AND (
      events.claim_id IS NULL
      OR EXISTS (
        SELECT 1 FROM claims AS mm_claim
        WHERE mm_claim.id = events.claim_id
          AND mm_claim.tenant_id = NULLIF(current_setting('memorymaster.tenant_id', true), '')
      )
    )
);
""".strip()


def _row_value(row, key: str, index: int):
    return row.get(key) if isinstance(row, dict) else row[index]


def _text(value) -> str | None:
    return None if value is None else str(value)


def _canonical_payload(payload: object | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        raw = payload.strip()
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_created_at(value: object, *, normalize_utc: bool = True) -> str:
    if not isinstance(value, datetime):
        return str(value)
    normalized = value
    if normalize_utc and value.tzinfo is not None:
        normalized = value.astimezone(timezone.utc)
    return normalized.replace(microsecond=0).isoformat()


def _compute_primary_event_hash(
    row,
    *,
    hash_algo: str,
    previous: str | None,
    normalize_utc: bool = True,
) -> str:
    tenant_id = _text(_row_value(row, "tenant_id", 11))
    components = [hash_algo]
    if hash_algo == _TENANT_EVENT_HASH_ALGO:
        if tenant_id is None:
            raise RuntimeError("Invalid primary event chain: tenant-v2 event has no tenant.")
        components.append(tenant_id)
    claim_id = _row_value(row, "claim_id", 1)
    components.extend(
        [
            str(claim_id) if claim_id is not None else "",
            str(_row_value(row, "event_type", 2)),
            _text(_row_value(row, "from_status", 3)) or "",
            _text(_row_value(row, "to_status", 4)) or "",
            _text(_row_value(row, "details", 5)) or "",
            _canonical_payload(_row_value(row, "payload_json", 6)),
            _canonical_created_at(
                _row_value(row, "created_at", 7),
                normalize_utc=normalize_utc,
            ),
            previous or "",
        ]
    )
    return hashlib.sha256("\x1f".join(components).encode("utf-8")).hexdigest()


def _compute_tenant_event_hash_v2(
    *,
    tenant_id: str,
    event_hash: str,
    tenant_prev_event_hash: str | None,
) -> str:
    material = "\x1f".join(
        (
            _TENANT_EVENT_HASH_ALGO,
            tenant_id,
            event_hash,
            tenant_prev_event_hash or "",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _validate_primary_chain(rows) -> None:
    global_head: str | None = None
    tenant_heads: dict[str, str | None] = {}
    for row in rows:
        event_id = int(_row_value(row, "id", 0))
        tenant_id = _text(_row_value(row, "tenant_id", 11))
        hash_algo = _text(_row_value(row, "hash_algo", 10)) or _EVENT_HASH_ALGO
        if hash_algo == _EVENT_HASH_ALGO:
            previous = global_head
        elif hash_algo == _TENANT_EVENT_HASH_ALGO and tenant_id is not None:
            previous = tenant_heads.get(tenant_id)
        else:
            raise RuntimeError(f"Invalid primary event chain algorithm at event {event_id}.")
        stored_previous = _text(_row_value(row, "prev_event_hash", 8))
        stored_hash = _text(_row_value(row, "event_hash", 9))
        expected_hashes = {
            _compute_primary_event_hash(
                row,
                hash_algo=hash_algo,
                previous=previous,
            )
        }
        if hash_algo == _EVENT_HASH_ALGO:
            expected_hashes.add(
                _compute_primary_event_hash(
                    row,
                    hash_algo=hash_algo,
                    previous=previous,
                    normalize_utc=False,
                )
            )
        if stored_previous != previous or stored_hash not in expected_hashes:
            raise RuntimeError(f"Invalid primary event chain at event {event_id}.")
        if hash_algo == _EVENT_HASH_ALGO:
            global_head = stored_hash
        else:
            tenant_heads[tenant_id] = stored_hash


def _tenant_hash_updates(rows) -> list[tuple[str | None, str, str, int]]:
    heads: dict[str, str | None] = {}
    updates: list[tuple[str | None, str, str, int]] = []
    for row in rows:
        tenant_id = _text(_row_value(row, "tenant_id", 11))
        if tenant_id is None:
            continue
        event_id = int(_row_value(row, "id", 0))
        event_hash = _text(_row_value(row, "event_hash", 9))
        if event_hash is None:
            raise RuntimeError("Cannot build tenant event chain before primary hashes exist.")
        previous = heads.get(tenant_id)
        expected_hash = _compute_tenant_event_hash_v2(
            tenant_id=tenant_id,
            event_hash=event_hash,
            tenant_prev_event_hash=previous,
        )
        stored_previous = _text(_row_value(row, "tenant_prev_event_hash", 12))
        stored_hash = _text(_row_value(row, "tenant_event_hash", 13))
        stored_algo = _text(_row_value(row, "tenant_hash_algo", 14))
        if stored_hash is not None:
            if (
                stored_previous != previous
                or stored_hash != expected_hash
                or stored_algo != _TENANT_EVENT_HASH_ALGO
            ):
                raise RuntimeError(f"Invalid tenant event chain at event {event_id}.")
        else:
            if stored_previous is not None or stored_algo is not None:
                raise RuntimeError(f"Invalid tenant event chain prefix at event {event_id}.")
            updates.append((previous, expected_hash, _TENANT_EVENT_HASH_ALGO, event_id))
        heads[tenant_id] = expected_hash
    return updates


def _add_sqlite_columns(conn) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    for column in (
        "tenant_id",
        "tenant_prev_event_hash",
        "tenant_event_hash",
        "tenant_hash_algo",
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE events ADD COLUMN {column} TEXT")


def _backfill_sqlite(conn) -> None:
    rows = conn.execute(_EVENT_ROWS_SQL).fetchall()
    _validate_primary_chain(rows)
    has_claims = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'claims'"
    ).fetchone()
    if has_claims is not None:
        conn.execute(
            """
            UPDATE events
            SET tenant_id = (
                SELECT claims.tenant_id FROM claims WHERE claims.id = events.claim_id
            )
            WHERE claim_id IS NOT NULL AND tenant_id IS NULL
            """
        )
    rows = conn.execute(_EVENT_ROWS_SQL).fetchall()
    updates = _tenant_hash_updates(rows)
    if updates:
        conn.executemany(
            """
            UPDATE events
            SET tenant_prev_event_hash = ?, tenant_event_hash = ?, tenant_hash_algo = ?
            WHERE id = ?
            """,
            updates,
        )


def apply_sqlite(conn) -> None:
    has_events = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()
    if has_events is None:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        _add_sqlite_columns(conn)
        conn.execute("DROP TRIGGER IF EXISTS trg_events_append_only_update")
        conn.execute("DROP TRIGGER IF EXISTS trg_events_append_only_delete")
        _backfill_sqlite(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_tenant_id ON events(tenant_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_tenant_hash ON events(tenant_id, tenant_event_hash)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_tenant_head ON events(tenant_id, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_tenant_algo_head "
            "ON events(tenant_id, hash_algo, id DESC)"
        )
        for statement in _SQLITE_APPEND_TRIGGER_STATEMENTS:
            conn.execute(statement)
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def apply_postgres(conn) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(_POSTGRES_PREPARE_DDL)
            cur.execute(_EVENT_ROWS_SQL)
            _validate_primary_chain(cur.fetchall())
            cur.execute(
                """
                UPDATE events AS event
                SET tenant_id = claim.tenant_id
                FROM claims AS claim
                WHERE event.claim_id = claim.id
                  AND event.tenant_id IS NULL
                """
            )
            cur.execute(_EVENT_ROWS_SQL)
            updates = _tenant_hash_updates(cur.fetchall())
            if updates:
                cur.executemany(
                    """
                    UPDATE events
                    SET tenant_prev_event_hash = %s,
                        tenant_event_hash = %s,
                        tenant_hash_algo = %s
                    WHERE id = %s
                    """,
                    updates,
                )
            cur.execute(_POSTGRES_FINALIZE_DDL)
        conn.commit()
    except Exception:
        rollback = getattr(conn, "rollback", None)
        if callable(rollback):
            rollback()
        raise
