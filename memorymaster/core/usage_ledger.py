"""Shared durable quota reservations for paid calls and external intake."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.stores._storage_shared import open_conn


class UsageQuotaExceeded(RuntimeError):
    def __init__(self, partition: str, limit: int) -> None:
        self.partition = partition
        self.limit = limit
        super().__init__(f"durable quota exhausted: partition={partition} limit={limit}")


@dataclass(frozen=True, slots=True)
class UsageReservation:
    reservation_id: str
    units: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_reservations (
    reservation_id TEXT PRIMARY KEY,
    window_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    provider TEXT NOT NULL,
    actor_hash TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units > 0),
    outcome TEXT NOT NULL DEFAULT 'reserved',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_window_operation
    ON usage_reservations(window_key, operation);
CREATE INDEX IF NOT EXISTS idx_usage_window_provider
    ON usage_reservations(window_key, operation, provider);
CREATE INDEX IF NOT EXISTS idx_usage_window_actor
    ON usage_reservations(window_key, operation, actor_hash);
"""


def _actor_hash(actor: str) -> str:
    return hashlib.sha256((actor or "unknown").encode("utf-8")).hexdigest()


def _env_int(name: str) -> int:
    try:
        return max(0, int(os.environ.get(name, "0")))
    except ValueError:
        return 0


class UsageLedger:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_conn(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def reserve(
        self,
        *,
        operation: str,
        provider: str,
        actor: str,
        units: int = 1,
        global_limit: int = 0,
        provider_limit: int = 0,
        actor_limit: int = 0,
        window_key: str | None = None,
    ) -> UsageReservation:
        units = max(1, int(units))
        window = window_key or datetime.now(timezone.utc).date().isoformat()
        provider = (provider or "unknown").strip().lower()
        actor_digest = _actor_hash(actor)
        conn = open_conn(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            partitions = (
                ("global", global_limit, "", (window, operation)),
                ("provider", provider_limit, " AND provider=?", (window, operation, provider)),
                ("actor", actor_limit, " AND actor_hash=?", (window, operation, actor_digest)),
            )
            for name, limit, suffix, params in partitions:
                if limit <= 0:
                    continue
                current = int(
                    conn.execute(
                        "SELECT COALESCE(SUM(units), 0) FROM usage_reservations "
                        f"WHERE window_key=? AND operation=?{suffix}",
                        params,
                    ).fetchone()[0]
                )
                if current + units > limit:
                    conn.rollback()
                    raise UsageQuotaExceeded(name, limit)
            reservation = UsageReservation(uuid.uuid4().hex, units)
            conn.execute(
                "INSERT INTO usage_reservations "
                "(reservation_id, window_key, operation, provider, actor_hash, units, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    reservation.reservation_id,
                    window,
                    operation,
                    provider,
                    actor_digest,
                    units,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            return reservation
        finally:
            conn.close()

    def finish(self, reservation: UsageReservation, *, outcome: str) -> None:
        with open_conn(self.db_path) as conn:
            conn.execute(
                "UPDATE usage_reservations SET outcome=?, finished_at=? "
                "WHERE reservation_id=?",
                (
                    outcome,
                    datetime.now(timezone.utc).isoformat(),
                    reservation.reservation_id,
                ),
            )


def configured_ledger() -> UsageLedger | None:
    configured = os.environ.get("MEMORYMASTER_USAGE_LEDGER_DB", "").strip()
    cap_names = (
        "MEMORYMASTER_MAX_LLM_CALLS_PER_DAY",
        "MEMORYMASTER_MAX_PROVIDER_CALLS_PER_DAY",
        "MEMORYMASTER_MAX_EMBEDDING_ITEMS_PER_DAY",
        "MEMORYMASTER_MAX_MCP_INGESTS_PER_DAY",
        "MEMORYMASTER_MAX_MCP_INGESTS_PER_AGENT_PER_DAY",
    )
    if not configured and not any(_env_int(name) for name in cap_names):
        return None
    path = Path(configured) if configured else Path.home() / ".memorymaster" / "usage-ledger.db"
    return UsageLedger(path)


def reserve_configured(
    *, operation: str, provider: str, actor: str, units: int = 1
) -> tuple[UsageLedger, UsageReservation] | None:
    ledger = configured_ledger()
    if ledger is None:
        return None
    if operation == "llm":
        global_limit = _env_int("MEMORYMASTER_MAX_LLM_CALLS_PER_DAY")
        provider_limit = _env_int("MEMORYMASTER_MAX_PROVIDER_CALLS_PER_DAY")
        actor_limit = 0
    elif operation == "embedding":
        global_limit = _env_int("MEMORYMASTER_MAX_EMBEDDING_ITEMS_PER_DAY")
        provider_limit = global_limit
        actor_limit = 0
    else:
        global_limit = _env_int("MEMORYMASTER_MAX_MCP_INGESTS_PER_DAY")
        provider_limit = 0
        actor_limit = _env_int("MEMORYMASTER_MAX_MCP_INGESTS_PER_AGENT_PER_DAY")
    reservation = ledger.reserve(
        operation=operation,
        provider=provider,
        actor=actor,
        units=units,
        global_limit=global_limit,
        provider_limit=provider_limit,
        actor_limit=actor_limit,
    )
    return ledger, reservation


def reserve_intake_configured(
    *, actor: str, units: int, actor_limit: int, window_key: str
) -> bool:
    """Use the shared ledger for intake when an explicit ledger is configured."""
    configured = os.environ.get("MEMORYMASTER_USAGE_LEDGER_DB", "").strip()
    if not configured:
        return False
    UsageLedger(configured).reserve(
        operation="ingest",
        provider="core",
        actor=actor,
        units=units,
        actor_limit=actor_limit,
        window_key=window_key,
    )
    return True
