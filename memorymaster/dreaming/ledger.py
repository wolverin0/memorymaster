"""Private auxiliary SQLite ledger for replayable Dreaming work."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from memorymaster.dreaming import held
from memorymaster.dreaming.models import CaptureEnvelope
from memorymaster.stores._storage_shared import connect_ro, open_conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS dream_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_key TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL,
    provider TEXT NOT NULL,
    session_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    messages_json TEXT NOT NULL,
    cursor_start INTEGER NOT NULL,
    cursor_end INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    turn_count INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'captured',
    extraction_json TEXT,
    decisions_json TEXT,
    run_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dream_captures_state_activity
    ON dream_captures(state, last_activity_at);
CREATE TABLE IF NOT EXISTS dream_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    extractor_model TEXT NOT NULL,
    consolidator_model TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS dream_provider_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    outcome TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    structured_valid INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dream_applications (
    application_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    capture_id INTEGER NOT NULL,
    candidate_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_claim_id INTEGER,
    created_claim_id INTEGER,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dream_hook_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    error_code TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dream_consolidation_trips (
    capture_id INTEGER PRIMARY KEY,
    trips INTEGER NOT NULL DEFAULT 0,
    solo_trips INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dream_leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    for key in ("messages_json", "extraction_json", "decisions_json", "summary_json"):
        if key in payload:
            raw = payload.pop(key)
            payload[key.removesuffix("_json")] = json.loads(raw) if raw else None
    return payload


class DreamLedger:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            from memorymaster.dreaming.ledger_migrations import migrate

            migrate(conn)

    def _connect(self) -> sqlite3.Connection:
        return open_conn(self.db_path, busy_ms=15_000)

    @staticmethod
    def _coalesce_captured(
        conn: sqlite3.Connection, envelope: CaptureEnvelope, now: str,
    ) -> int | None:
        latest = conn.execute(
            """SELECT id, cursor_start, cursor_end, messages_json
               FROM dream_captures
               WHERE provider=? AND session_hash=? AND scope=? AND state='captured'
               ORDER BY cursor_end DESC, id DESC LIMIT 1""",
            (envelope.provider, envelope.session_hash, envelope.scope),
        ).fetchone()
        if latest is None:
            return None
        incoming = [message.to_dict() for message in envelope.messages]
        persisted = json.loads(str(latest["messages_json"]))
        incoming_ids = {str(message["message_id"]) for message in incoming}
        persisted_ids = {str(message["message_id"]) for message in persisted}
        if envelope.cursor_end <= int(latest["cursor_end"]) and incoming_ids <= persisted_ids:
            return int(latest["id"])
        if int(latest["cursor_end"]) != envelope.cursor_start:
            return None
        merged = [
            *persisted,
            *(message for message in incoming if str(message["message_id"]) not in persisted_ids),
        ]
        merged_json = json.dumps(merged, ensure_ascii=False)
        merged_hash = hashlib.sha256(
            json.dumps(merged, sort_keys=True).encode("utf-8"),
        ).hexdigest()
        conn.execute(
            """UPDATE dream_captures
               SET last_activity_at=?, messages_json=?, cursor_end=?, content_hash=?,
                   byte_count=?, turn_count=?, updated_at=? WHERE id=?""",
            (
                envelope.last_activity_at, merged_json, envelope.cursor_end, merged_hash,
                len(merged_json.encode("utf-8")), len(merged), now, int(latest["id"]),
            ),
        )
        return int(latest["id"])

    def enqueue(self, envelope: CaptureEnvelope) -> int:
        messages_json = json.dumps([message.to_dict() for message in envelope.messages], ensure_ascii=False)
        capture_key = ":".join((envelope.provider, envelope.session_hash, str(envelope.cursor_start), str(envelope.cursor_end), envelope.content_hash))
        now = _iso(_utc_now())
        with self._connect() as conn:
            exact = conn.execute(
                "SELECT id FROM dream_captures WHERE capture_key=?", (capture_key,),
            ).fetchone()
            if exact is not None:
                return int(exact[0])
            coalesced = self._coalesce_captured(conn, envelope, now)
            if coalesced is not None:
                return coalesced
            conn.execute(
                """INSERT OR IGNORE INTO dream_captures
                   (capture_key, version, provider, session_hash, scope, captured_at,
                    last_activity_at, messages_json, cursor_start, cursor_end,
                    content_hash, byte_count, turn_count, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (capture_key, envelope.version, envelope.provider, envelope.session_hash,
                 envelope.scope, envelope.captured_at, envelope.last_activity_at,
                 messages_json, envelope.cursor_start, envelope.cursor_end,
                 envelope.content_hash, len(messages_json.encode("utf-8")),
                 len(envelope.messages), now),
            )
            row = conn.execute("SELECT id FROM dream_captures WHERE capture_key=?", (capture_key,)).fetchone()
        return int(row[0])

    def get_capture(self, capture_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM dream_captures WHERE id=?", (capture_id,)).fetchone()
        if row is None:
            raise KeyError(capture_id)
        return _decode_row(row)

    def eligible(self, *, idle_minutes: int, max_sessions: int, scope: str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
        cutoff = _iso((now or _utc_now()) - timedelta(minutes=max(0, idle_minutes)))
        params: list[Any] = [cutoff]
        where = "(state IN ('captured','retryable') OR (state='extracted' AND resume_eligible=1)) AND turn_count >= 2 AND last_activity_at <= ?"
        if scope:
            where += " AND scope=?"
            params.append(scope)
        params.append(max(0, max_sessions))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM dream_captures WHERE {where} ORDER BY last_activity_at, id LIMIT ?", params,
            ).fetchall()
        return [_decode_row(row) for row in rows]

    def consolidated(self, *, max_sessions: int, scope: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = "state='consolidated'"
        if scope:
            where += " AND scope=?"
            params.append(scope)
        params.append(max(0, max_sessions))
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM dream_captures WHERE {where} ORDER BY id LIMIT ?", params).fetchall()
        return [_decode_row(row) for row in rows]

    def set_extraction(self, capture_id: int, candidates: Iterable[dict[str, Any]], run_id: str) -> None:
        self._set_capture(capture_id, state="extracted", run_id=run_id, extraction_json=json.dumps(list(candidates), ensure_ascii=False), error=None)

    def set_decisions(self, capture_id: int, decisions: Iterable[dict[str, Any]], run_id: str, *, retry_error: str | None = None) -> None:
        # Reusing the same reviewed decisions must retain application retry state.
        self._set_capture(capture_id, state="consolidated", run_id=run_id, decisions_json=json.dumps(list(decisions), ensure_ascii=False), error=retry_error)

    def mark_applied(self, capture_id: int, run_id: str) -> None:
        self._set_capture(capture_id, state="applied", run_id=run_id, error=None)

    def mark_retryable(self, capture_id: int, run_id: str, error: str, *, error_count: int | None = None) -> None:
        self._set_capture(capture_id, state="retryable", run_id=run_id, error=error[:500], error_count=error_count)

    def mark_quarantined(self, capture_id: int, run_id: str, error: str, *, error_count: int | None = None) -> None:
        self._set_capture(capture_id, state="quarantined", run_id=run_id, error=error[:500], error_count=error_count)

    def defer_consolidation(self, capture_id: int, run_id: str) -> None:
        # A budget deferral is not a stage success: keep last_error so the
        # semantic-failure count survives until consolidation succeeds.
        with self._connect() as conn:
            conn.execute(
                """UPDATE dream_captures
                   SET state='extracted', run_id=?, updated_at=?, deferred_reason='consolidate_budget'
                   WHERE id=? AND extraction_json IS NOT NULL""",
                (run_id, _iso(_utc_now()), capture_id),
            )

    def held_capture_ids(self, capture_id: int | None = None) -> list[int]:
        """Captures that still hold at least one Jev-held candidate (S3 INGEST)."""
        where, params = ("held_count>0 AND id=?", (int(capture_id),)) if capture_id is not None else ("held_count>0", ())
        with self._connect() as conn:
            rows = conn.execute(f"SELECT id FROM dream_captures WHERE {where} ORDER BY id", params).fetchall()
        return [int(row[0]) for row in rows]

    def update_extraction(
        self,
        capture_id: int,
        change: Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], int, bool] | None],
        *,
        keep_states: Iterable[str] = (),
    ) -> list[dict[str, Any]] | None:
        """Read-modify-write one capture's extraction annotations in one transaction.

        ``change(candidates)`` returns ``None`` (write nothing) or
        ``(candidates, held_count, reopen)``. ``reopen`` moves a consolidated or
        applied capture back to ``extracted`` so the next run consolidates the
        candidates it no longer holds. Other capture fields are untouched. A
        capture whose state is in ``keep_states`` is left as it is (``None``).
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT extraction_json, state FROM dream_captures WHERE id=?",
                               (int(capture_id),)).fetchone()
            kept = row is None or not row[0] or row[1] in set(keep_states)
            outcome = None if kept else change(json.loads(row[0]))
            if outcome is None:
                conn.rollback()
                return None
            candidates, held, reopen = outcome
            assignments = ["extraction_json=?", "held_count=?"]
            values: list[Any] = [json.dumps(candidates, ensure_ascii=False), max(0, int(held))]
            if reopen and row[1] in {"consolidated", "applied"}:
                assignments += ["state='extracted'", "resume_eligible=1", "updated_at=?"]
                values.append(_iso(_utc_now()))
            conn.execute(f"UPDATE dream_captures SET {', '.join(assignments)} WHERE id=?",
                         (*values, int(capture_id)))
            conn.commit()
        return candidates

    def consolidation_trips(self, capture_ids: Iterable[int]) -> dict[int, tuple[int, int]]:
        """Ruling R2 streaks ``(trips, solo_trips)`` for captures that have one."""
        ids = sorted({int(capture_id) for capture_id in capture_ids})
        if not ids:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT capture_id, trips, solo_trips FROM dream_consolidation_trips "
                f"WHERE capture_id IN ({','.join('?' for _ in ids)})",
                ids,
            ).fetchall()
        return {int(row[0]): (int(row[1]), int(row[2])) for row in rows}

    def record_consolidation_trip(
        self, capture_ids: Iterable[int], *, alone: bool, now: datetime | None = None,
    ) -> dict[int, tuple[int, int]]:
        """Count a consolidation call that tripped the breaker (ruling R2).

        Every capture in the call gets one more consecutive trip; ``alone``
        (its isolated call failed after the provider answered another batch in
        the run) also extends its lone-trip streak. The capture row itself is
        left untouched. Returns the new ``(trips, solo_trips)`` per capture.
        """
        ids = sorted({int(capture_id) for capture_id in capture_ids})
        if not ids:
            return {}
        timestamp = _iso(now or _utc_now())
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO dream_consolidation_trips (capture_id, trips, solo_trips, updated_at)
                   VALUES (?, 1, ?, ?)
                   ON CONFLICT(capture_id) DO UPDATE SET trips=trips+1,
                       solo_trips=solo_trips+excluded.solo_trips, updated_at=excluded.updated_at""",
                [(capture_id, int(alone), timestamp) for capture_id in ids],
            )
        return self.consolidation_trips(ids)

    def clear_consolidation_trips(self, capture_ids: Iterable[int]) -> None:
        """A completed consolidation call ends the captures' trip streaks."""
        ids = sorted({int(capture_id) for capture_id in capture_ids})
        if ids:
            with self._connect() as conn:
                conn.execute(
                    f"DELETE FROM dream_consolidation_trips WHERE capture_id IN ({','.join('?' for _ in ids)})",
                    ids,
                )

    def _set_capture(self, capture_id: int, *, state: str, run_id: str, error: str | None, extraction_json: str | None = None, decisions_json: str | None = None, error_count: int | None = None) -> None:
        assignments = ["state=?", "run_id=?", "last_error=?", "updated_at=?", "attempts=attempts+1", "deferred_reason=NULL"]
        values: list[Any] = [state, run_id, error, _iso(_utc_now())]
        if error_count is not None:
            assignments.append("error_count=?")
            values.append(max(0, int(error_count)))
        if extraction_json is not None:
            assignments.extend(["extraction_json=?", "resume_eligible=1", "extraction_run_id=?", "held_count=0"])
            values.extend([extraction_json, run_id])
        if decisions_json is not None:
            assignments.append("decisions_json=?")
            values.append(decisions_json)
        values.append(capture_id)
        with self._connect() as conn:
            if state in {"consolidated", "applied"}:
                # S3: held.release() does not wait for a running worker. A
                # candidate released after the run read this capture has no
                # decision yet, so the capture is not done: keep it resumable
                # (the next run consolidates just that candidate) instead of
                # closing it, and retention pruning it, without the candidate.
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT extraction_json, decisions_json FROM dream_captures WHERE id=?",
                                   (capture_id,)).fetchone()
                if row is not None and row[0] and held.released_undecided(
                    json.loads(row[0]), json.loads(decisions_json or row[1] or "[]"),
                ):
                    values[0] = "extracted"
                    assignments.append("resume_eligible=1")
            conn.execute(f"UPDATE dream_captures SET {', '.join(assignments)} WHERE id=?", values)

    def acquire_lease(self, name: str, owner: str, ttl_seconds: int, *, now: datetime | None = None) -> bool:
        current = now or _utc_now()
        expires = current + timedelta(seconds=max(1, ttl_seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner, expires_at FROM dream_leases WHERE name=?", (name,)).fetchone()
            if row and row[0] != owner and str(row[1]) > _iso(current):
                conn.rollback()
                return False
            conn.execute(
                """INSERT INTO dream_leases VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,
                   acquired_at=excluded.acquired_at, heartbeat_at=excluded.heartbeat_at,
                   expires_at=excluded.expires_at""",
                (name, owner, _iso(current), _iso(current), _iso(expires)),
            )
            conn.commit()
        return True

    def renew_lease(self, name: str, owner: str, ttl_seconds: int, *, now: datetime | None = None) -> bool:
        """Extend a lease this owner still holds; False once anyone else took it.

        A missing row is a lost lease too: another worker may have acquired,
        finished and released it while this one was stalled.
        """
        current = now or _utc_now()
        expires = current + timedelta(seconds=max(1, ttl_seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner FROM dream_leases WHERE name=?", (name,)).fetchone()
            if row is None or row[0] != owner:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE dream_leases SET heartbeat_at=?, expires_at=? WHERE name=? AND owner=?",
                (_iso(current), _iso(expires), name, owner),
            )
            conn.commit()
        return True

    def release_lease(self, name: str, owner: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM dream_leases WHERE name=? AND owner=?", (name, owner))

    def start_run(self, dry_run: bool, extractor_model: str, consolidator_model: str, *, now: datetime | None = None) -> str:
        run_id = "dream-" + uuid.uuid4().hex
        timestamp = _iso(now or _utc_now())
        with self._connect() as conn:
            conn.execute("INSERT INTO dream_runs VALUES (?, 'running', ?, ?, ?, ?, ?, NULL, NULL)",
                         (run_id, int(dry_run), extractor_model, consolidator_model, timestamp, timestamp))
        return run_id

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any], *, now: datetime | None = None) -> None:
        timestamp = _iso(now or _utc_now())
        with self._connect() as conn:
            conn.execute("UPDATE dream_runs SET status=?, heartbeat_at=?, completed_at=?, summary_json=? WHERE run_id=?",
                         (status, timestamp, timestamp, json.dumps(summary, ensure_ascii=False), run_id))

    def abandon_stale_runs(
        self, current_run_id: str, *, now: datetime | None = None,
    ) -> int:
        timestamp = _iso(now or _utc_now())
        summary = json.dumps(
            {"ok": False, "reason": "stale_lease_recovered"},
            separators=(",", ":"),
        )
        with self._connect() as conn:
            result = conn.execute(
                """UPDATE dream_runs
                   SET status='abandoned', heartbeat_at=?, completed_at=?,
                       summary_json=?
                   WHERE status='running' AND run_id<>? AND heartbeat_at<?""",
                (timestamp, timestamp, summary, current_run_id, timestamp),
            )
        return max(0, int(result.rowcount))

    def record_provider_call(self, run_id: str, *, provider: str, model: str, outcome: str, latency_ms: int, structured_valid: bool, input_tokens: int, output_tokens: int, http_status: int, now: datetime | None = None) -> None:
        with self._connect() as conn:
            conn.execute("""INSERT INTO dream_provider_usage
                (run_id, provider, model, outcome, http_status, latency_ms, structured_valid,
                 input_tokens, output_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, provider, model, outcome, http_status, max(0, latency_ms), int(structured_valid),
                 max(0, input_tokens), max(0, output_tokens), _iso(now or _utc_now())))

    def provider_calls_today(
        self,
        provider: str,
        *,
        model: str | None = None,
        now: datetime | None = None,
    ) -> int:
        day = (now or _utc_now()).astimezone(timezone.utc).date().isoformat()
        model_filter = " AND model=?" if model else ""
        params = (provider, day, model) if model else (provider, day)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM dream_provider_usage "
                "WHERE provider=? AND substr(created_at,1,10)=?"
                + model_filter,
                params,
            ).fetchone()
        return int(row[0])

    def provider_input_tokens_today(
        self,
        provider: str,
        *,
        model: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Tokens de ENTRADA gastados hoy por ese proveedor.

        Existe porque `provider_calls_today` NO acota gasto: cuenta llamadas, y el
        costo por llamada varia por un factor grande segun el transporte. Medido
        el 2026-08-24 sobre este mismo ledger: 527 llamadas a openai gastaron
        9.762.549 tokens de entrada —unos 18,5k por llamada— mientras que 540 a
        google gastaron 3.889.063, cerca de 7,2k. El mismo tope de llamadas
        permite dos gastos que difieren en 2,5x.

        Se miden los de ENTRADA porque son el volumen dominante: en el mismo
        ledger, entrada 17,4M contra salida 344k.
        """
        day = (now or _utc_now()).astimezone(timezone.utc).date().isoformat()
        model_filter = " AND model=?" if model else ""
        params = (provider, day, model) if model else (provider, day)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(input_tokens), 0) FROM dream_provider_usage "
                "WHERE provider=? AND substr(created_at,1,10)=?"
                + model_filter,
                params,
            ).fetchone()
        return int(row[0])

    def application_exists(self, application_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM dream_applications WHERE application_key=?", (application_key,)).fetchone()
        return row is not None

    def record_application(self, application_key: str, *, run_id: str, capture_id: int, candidate_id: str, action: str, target_claim_id: int | None, created_claim_id: int | None, now: datetime | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO dream_applications
                   (application_key, run_id, capture_id, candidate_id, action,
                    target_claim_id, created_claim_id, applied_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (application_key, run_id, capture_id, candidate_id, action,
                 target_claim_id, created_claim_id, _iso(now or _utc_now())),
            )

    def candidate_writes_today(self, *, now: datetime | None = None) -> int:
        day = (now or _utc_now()).astimezone(timezone.utc).date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM dream_applications
                   WHERE substr(applied_at,1,10)=?
                     AND action IN ('add','reinforce','propose_supersede','propose_conflict')""",
                (day,),
            ).fetchone()
        return int(row[0])

    def record_hook_error(self, provider: str, error_code: str, *, now: datetime | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO dream_hook_errors (provider, error_code, occurred_at) VALUES (?, ?, ?)",
                (provider[:50], error_code[:100], _iso(now or _utc_now())),
            )

    def status(
        self,
        *,
        now: datetime | None = None,
        interval_minutes: int = 60,
        provider_window_hours: int = 24,
    ) -> dict[str, Any]:
        current = now or _utc_now()
        with self._connect() as conn:
            return self._status_from_connection(
                conn, current, interval_minutes, provider_window_hours,
            )

    @classmethod
    def read_status(
        cls,
        db_path: str | Path,
        *,
        now: datetime | None = None,
        interval_minutes: int = 60,
        provider_window_hours: int = 24,
    ) -> dict[str, Any]:
        """Read status through SQLite query-only mode; never create or migrate a ledger."""
        path = Path(db_path)
        current = now or _utc_now()
        empty_window = cls._provider_window({}, current, provider_window_hours)
        if not path.is_file():
            return {
                "queue": {}, "last_run": None, "providers": {}, "leases": [],
                "providers_lifetime": {}, "provider_window": empty_window,
                "hook_errors": 0, "warnings": ["scheduler_stale"],
            }
        with connect_ro(path) as conn:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            required = {
                "dream_captures", "dream_runs", "dream_provider_usage",
                "dream_leases", "dream_hook_errors",
            }
            if not required <= tables:
                return {
                    "queue": {}, "last_run": None, "providers": {}, "leases": [],
                    "providers_lifetime": {}, "provider_window": empty_window,
                    "hook_errors": 0, "warnings": ["dream_schema_missing"],
                }
            return cls._status_from_connection(
                conn, current, interval_minutes, provider_window_hours,
            )

    @classmethod
    def _status_from_connection(
        cls,
        conn: sqlite3.Connection,
        current: datetime,
        interval_minutes: int,
        provider_window_hours: int,
    ) -> dict[str, Any]:
        queue = {str(row[0]): int(row[1]) for row in conn.execute("SELECT state, COUNT(*) FROM dream_captures GROUP BY state")}
        last = conn.execute("SELECT * FROM dream_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        usage_sql = """SELECT provider, COUNT(*), SUM(structured_valid), SUM(input_tokens), SUM(output_tokens), SUM(CASE WHEN http_status=429 THEN 1 ELSE 0 END)
            FROM dream_provider_usage {where_clause} GROUP BY provider"""
        lifetime_usage = conn.execute(usage_sql.format(where_clause="")).fetchall()
        window_start = current - timedelta(hours=max(1, provider_window_hours))
        recent_usage = conn.execute(
            usage_sql.format(where_clause="WHERE created_at >= ?"), (_iso(window_start),),
        ).fetchall()
        leases = [dict(row) for row in conn.execute("SELECT * FROM dream_leases")]
        hook_errors = int(conn.execute("SELECT COUNT(*) FROM dream_hook_errors").fetchone()[0])
        providers_lifetime = cls._provider_stats(lifetime_usage)
        providers_recent = cls._provider_stats(recent_usage)
        warnings = cls._warnings(last, providers_recent, current, interval_minutes)
        return {
            "queue": queue,
            "pending_extractions": cls._pending_extractions(conn),
            "held": cls._held(conn),
            "last_run": _decode_row(last) if last else None,
            "providers": providers_lifetime,
            "providers_lifetime": providers_lifetime,
            "provider_window": cls._provider_window(
                providers_recent, current, provider_window_hours,
            ),
            "leases": leases,
            "hook_errors": hook_errors,
            "warnings": warnings,
        }

    @staticmethod
    def _pending_extractions(conn: sqlite3.Connection) -> dict[str, int]:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(dream_captures)")}
        if "resume_eligible" not in columns:
            retained = conn.execute("SELECT COUNT(*) FROM dream_captures WHERE state='extracted'").fetchone()[0]
            return {"resumable": 0, "deferred_budget": 0, "historical_retained": int(retained)}
        row = conn.execute("""SELECT
            COALESCE(SUM(resume_eligible=1),0),
            COALESCE(SUM(resume_eligible=1 AND deferred_reason='consolidate_budget'),0),
            COALESCE(SUM(resume_eligible=0),0)
            FROM dream_captures WHERE state='extracted'""").fetchone()
        return dict(zip(("resumable", "deferred_budget", "historical_retained"), map(int, row)))

    @staticmethod
    def _held(conn: sqlite3.Connection) -> dict[str, int]:
        """S3 INGEST: Jev-held candidates awaiting release (quarantined captures are never released)."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(dream_captures)")}
        if "held_count" not in columns:
            return {"captures": 0, "candidates": 0, "quarantined_captures": 0}
        row = conn.execute("""SELECT COUNT(*), COALESCE(SUM(held_count),0),
            COALESCE(SUM(state='quarantined'),0) FROM dream_captures WHERE held_count>0""").fetchone()
        return dict(zip(("captures", "candidates", "quarantined_captures"), map(int, row)))

    @staticmethod
    def _provider_stats(rows: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
        return {
            str(row[0]): {
                "calls": int(row[1]),
                "structured_yield": float(row[2] or 0) / max(1, int(row[1])),
                "input_tokens": int(row[3] or 0),
                "output_tokens": int(row[4] or 0),
                "http_429": int(row[5] or 0),
            }
            for row in rows
        }

    @staticmethod
    def _provider_window(
        providers: dict[str, dict[str, Any]], current: datetime, hours: int,
    ) -> dict[str, Any]:
        bounded_hours = max(1, hours)
        return {
            "hours": bounded_hours,
            "started_at": _iso(current - timedelta(hours=bounded_hours)),
            "providers": providers,
        }

    @staticmethod
    def _warnings(last: sqlite3.Row | None, providers: dict[str, dict[str, Any]], now: datetime, interval_minutes: int) -> list[str]:
        warnings: list[str] = []
        if last is None or str(last["heartbeat_at"]) < _iso(now - timedelta(minutes=max(1, interval_minutes) * 2)):
            warnings.append("scheduler_stale")
        for provider, stats in providers.items():
            if stats["calls"] >= 10 and stats["structured_yield"] < 0.9:
                warnings.append(f"{provider}_structured_yield_low")
        return warnings

    def prune(
        self,
        *,
        retain_days: int,
        max_bytes: int,
        now: datetime | None = None,
        held_retain_days: int = held.HELD_RETAIN_DAYS,
        on_held_expired: Callable[[list[dict[str, Any]]], Any] | None = None,
    ) -> dict[str, Any]:
        """Delete applied/quarantined captures past ``retain_days`` or over ``max_bytes``.

        Ruling R7: a capture that still holds Jev-held candidates is exempt for
        at most ``held_retain_days`` after its latest hold; a quarantined capture
        is never exempt. Returns ``deleted`` and ``held_expired``, one entry per
        held candidate pruned with its capture (``decision_id``, ``item_ref``,
        ``capture_id``, ``held_since``, ``reason``). ``on_held_expired`` receives
        each capture's entries before it is deleted (the worker records the
        ``held_expired`` outcomes there); it never blocks the prune.
        """
        current = now or _utc_now()
        cutoff = _iso(current - timedelta(days=max(0, retain_days)))
        held_cutoff = current - timedelta(days=max(0, held_retain_days))
        deleted = 0
        expired: list[dict[str, Any]] = []
        with self._connect() as conn:
            exempt = self._held_exempt(conn, held_cutoff)
            keep = f" AND id NOT IN ({','.join('?' for _ in exempt)})" if exempt else ""
            rows = conn.execute("SELECT id FROM dream_captures WHERE state IN ('applied','quarantined') "
                                f"AND updated_at < ?{keep} ORDER BY updated_at, id", (cutoff, *exempt)).fetchall()
            for row in rows:
                expired.extend(self._delete_pruned(conn, int(row[0]), on_held_expired))
                deleted += 1
            total = int(conn.execute("SELECT COALESCE(SUM(byte_count),0) FROM dream_captures").fetchone()[0])
            while total > max(0, max_bytes):
                row = conn.execute("SELECT id, byte_count FROM dream_captures WHERE state IN ('applied','quarantined')"
                                   f"{keep} ORDER BY updated_at LIMIT 1", exempt).fetchone()
                if row is None:
                    break
                expired.extend(self._delete_pruned(conn, int(row[0]), on_held_expired))
                total -= int(row[1])
                deleted += 1
            if deleted:
                conn.execute("DELETE FROM dream_consolidation_trips "
                             "WHERE capture_id NOT IN (SELECT id FROM dream_captures)")
        return {"deleted": deleted, "held_expired": expired}

    @staticmethod
    def _held_exempt(conn: sqlite3.Connection, held_cutoff: datetime) -> list[int]:
        """Applied captures whose latest hold is newer than ``held_cutoff`` (ruling R7)."""
        rows = conn.execute("SELECT id, extraction_json, updated_at FROM dream_captures "
                            "WHERE held_count>0 AND state='applied'").fetchall()
        exempt = []
        for capture_id, extraction_json, updated_at in rows:
            since = held.held_since(json.loads(extraction_json or "[]"), fallback=updated_at)
            if since is None or since > held_cutoff:
                exempt.append(int(capture_id))
        return exempt

    @staticmethod
    def _delete_pruned(
        conn: sqlite3.Connection,
        capture_id: int,
        on_held_expired: Callable[[list[dict[str, Any]]], Any] | None,
    ) -> list[dict[str, Any]]:
        row = conn.execute("SELECT state, held_count, extraction_json, updated_at FROM dream_captures WHERE id=?",
                           (capture_id,)).fetchone()
        notes: list[dict[str, Any]] = []
        if row is not None and int(row[1] or 0) > 0:
            reason = "quarantined" if row[0] == "quarantined" else "held_retention_expired"
            notes = held.expired_notes(capture_id, json.loads(row[2] or "[]"), reason=reason, fallback=row[3])
            if notes and on_held_expired is not None:
                try:
                    on_held_expired(notes)
                except Exception:  # noqa: BLE001 - telemetry never blocks retention
                    pass
        conn.execute("DELETE FROM dream_captures WHERE id=?", (capture_id,))
        return notes
