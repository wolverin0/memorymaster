"""Append-only SQLite sidecar for Jev decisions (schema v1).

A separate database (default ``~/.memorymaster/decisions.db``) so decision
telemetry never competes for the authoritative store's write lock.  WAL,
``busy_timeout`` 15 s and ``BEGIN IMMEDIATE`` make appends safe across the hook
processes of several panes.  Inside :meth:`DecisionLedger.bounded` (the engine
uses it for hook decisions) every open, read and write of this thread that meets
another connection's lock is retried for at most the given bound (and never past
its ``until``), measured precisely rather than by SQLite's coarse busy-handler
sleeps, and opens skip the retry backoff.  WAL readers do not wait for a writer's
transaction, but they can still meet a lock while another hook process opens or
closes the database (WAL recovery, header reads), so reads are retried too.
Triggers enforce append-only at the database: rows are never updated or deleted,
except that the prune job may set ``decisions.state_redacted`` to NULL after the
retention window.

Every public write returns a status and never raises into the caller; failures
are counted (``ledger_write_failures``) and logged once per process.  A read the
engine needs before it may send (breaker, budget, thresholds) is counted there
too when it fails, and ``get_watermark(..., strict=True)`` tells a failed read
(:class:`LedgerReadError`) from an absent watermark (``None``).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import astuple, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from memorymaster.decisions.questions import QuestionSpec, default_thresholds
from memorymaster.stores._storage_shared import open_conn, open_conn_bounded

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 15_000
# After a failed open, fail fast for a while instead of paying open_conn's
# retry backoff on every call from a hook.
UNAVAILABLE_COOLDOWN_S = 60.0
_log = logging.getLogger(__name__)
_failure_lock = threading.Lock()
_failures = 0
_logged = False


def utc_iso(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class DecisionRecord:
    decision_id: str
    ts: str = field(default_factory=utc_iso)
    surface: str = ""
    mode: str = "off"
    policy_version: str | None = None
    question_set_id: str | None = None
    question_sha256: str | None = None
    primitive_summary: str | None = None
    model_requested: str | None = None
    model_served: str | None = None
    backend: str | None = None
    transport_version: str | None = None
    sdk_version: str | None = None
    code_revision: str | None = None
    state_schema_version: int | None = None
    state_sha256: str | None = None
    state_redacted: str | None = None
    egress_bytes: int | None = None
    redaction_counts_json: str | None = None
    transport_outcome: str | None = None
    fallback_reason: str | None = None
    latency_ms: int | None = None  # transport: request sent -> answer read
    engine_ms: int | None = None  # the whole decide() call, including the transport wait
    attempt_count: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    legacy_action: str | None = None
    jev_action: str | None = None
    available_actions_json: str | None = None
    action_taken: str | None = None
    exploration_arm: str | None = None
    action_propensities_json: str | None = None
    chosen_propensity: float | None = None
    randomization_id: str | None = None
    thresholds_json: str | None = None
    baseline_features_json: str | None = None
    session_key: str | None = None
    scope: str | None = None
    tenant: str | None = None


@dataclass
class ItemRecord:
    decision_id: str
    item_ref: str
    item_kind: str | None = None
    question_id: str = ""
    question_version: int | None = None
    answer: str | None = None
    probabilities_json: str | None = None
    confidence: float | None = None
    rank_legacy: int | None = None
    rank_final: int | None = None
    exposed: int = 0
    delivered: int = 0


@dataclass
class OutcomeRecord:
    decision_id: str
    item_ref: str
    kind: str
    value: float | None = None
    was_exposed: int | None = None
    outcome_window: str | None = None
    reward_version: str | None = None
    label_source: str | None = None
    observed_at: str = field(default_factory=utc_iso)
    lag_s: int | None = None
    details_json: str | None = None


DECISION_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(DecisionRecord))
ITEM_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(ItemRecord))
OUTCOME_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(OutcomeRecord))

_TYPES = {
    "state_schema_version": "INT", "egress_bytes": "INT", "latency_ms": "INT", "engine_ms": "INT",
    "attempt_count": "INT",
    "tokens_in": "INT", "tokens_out": "INT", "cost_usd": "REAL", "chosen_propensity": "REAL",
}
_DECISIONS_DDL = "CREATE TABLE IF NOT EXISTS decisions (\n    " + ",\n    ".join(
    f"{name} {_TYPES.get(name, 'TEXT')}" + (" PRIMARY KEY" if name == "decision_id" else "")
    for name in DECISION_COLUMNS
) + "\n)"
_IMMUTABLE_DECISION_COLUMNS = [c for c in DECISION_COLUMNS if c != "state_redacted"]

_SCHEMA = [
    _DECISIONS_DDL,
    """CREATE TABLE IF NOT EXISTS decision_items (
    decision_id TEXT, item_ref TEXT, item_kind TEXT, question_id TEXT, question_version INT,
    answer TEXT, probabilities_json TEXT, confidence REAL, rank_legacy INT, rank_final INT,
    exposed INT, delivered INT, PRIMARY KEY(decision_id, item_ref, question_id))""",
    """CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id INTEGER PRIMARY KEY, decision_id TEXT, item_ref TEXT, kind TEXT, value REAL,
    was_exposed INT, outcome_window TEXT, reward_version TEXT, label_source TEXT,
    observed_at TEXT, lag_s INT, details_json TEXT, UNIQUE(decision_id, item_ref, kind, observed_at))""",
    """CREATE TABLE IF NOT EXISTS question_versions (
    question_id TEXT, version INT, sha256 TEXT, primitive TEXT, text_json TEXT,
    threshold_json TEXT, created_at TEXT, PRIMARY KEY(question_id, version))""",
    "CREATE TABLE IF NOT EXISTS watermarks (name TEXT PRIMARY KEY, value TEXT, updated_at TEXT)",
    # Written BEFORE a request leaves: if the decision row later fails to write, the
    # request, its surface and estimated cost stay on record and still count against
    # the daily cap and RPM cap (an intent without a decision row is an orphan).
    """CREATE TABLE IF NOT EXISTS send_intents (
    decision_id TEXT PRIMARY KEY, surface TEXT, ts TEXT, est_cost_usd REAL)""",
    "CREATE INDEX IF NOT EXISTS idx_send_intents_ts ON send_intents(ts)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_surface_ts ON decisions(surface, ts)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_session_ts ON decisions(session_key, ts)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts)",
    "CREATE INDEX IF NOT EXISTS idx_decision_items_ref ON decision_items(item_ref)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_decision ON outcomes(decision_id, item_ref)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_kind_time ON outcomes(kind, observed_at)",
    "CREATE TRIGGER IF NOT EXISTS trg_decisions_no_delete BEFORE DELETE ON decisions "
    "BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_decisions_no_update BEFORE UPDATE ON decisions WHEN "
    + " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in _IMMUTABLE_DECISION_COLUMNS)
    + " OR (NEW.state_redacted IS NOT NULL AND NEW.state_redacted IS NOT OLD.state_redacted) "
    "BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_items_no_update BEFORE UPDATE ON decision_items "
    "BEGIN SELECT RAISE(ABORT, 'decision_items are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_items_no_delete BEFORE DELETE ON decision_items "
    "BEGIN SELECT RAISE(ABORT, 'decision_items are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_outcomes_no_update BEFORE UPDATE ON outcomes "
    "BEGIN SELECT RAISE(ABORT, 'outcomes are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_outcomes_no_delete BEFORE DELETE ON outcomes "
    "BEGIN SELECT RAISE(ABORT, 'outcomes are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_intents_no_update BEFORE UPDATE ON send_intents "
    "BEGIN SELECT RAISE(ABORT, 'send_intents are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_intents_no_delete BEFORE DELETE ON send_intents "
    "BEGIN SELECT RAISE(ABORT, 'send_intents are append-only'); END",
]
_SCHEMA_OBJECTS = frozenset(re.findall(r"IF NOT EXISTS (\w+)", "\n".join(_SCHEMA)))


_BOUNDED_POLL_S = 0.005


def _open_bounded(path: Path) -> sqlite3.Connection:
    """The ``open_conn`` envelope with no busy handler and no retry backoff.

    Waiting for another writer is done by :meth:`DecisionLedger._begin`, which
    measures time precisely (SQLite's busy handler sleeps in steps of the Windows
    timer, so a short busy_timeout can overshoot by several ticks).
    """
    return open_conn_bounded(path)


def _is_locked(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


class LedgerUnavailable(RuntimeError):
    """Internal: the ledger cannot be used (never escapes public methods)."""


class LedgerReadError(RuntimeError):
    """``DecisionLedger.query`` could not read an existing ledger (not the same as no rows)."""


def ledger_write_failures() -> int:
    """Process-wide count of failed ledger writes."""
    return _failures


class DecisionLedger:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.write_failures = 0
        self._ready = False
        self._unavailable_until = 0.0
        self._local = threading.local()

    @contextlib.contextmanager
    def bounded(self, busy_timeout_ms: int, *, until: float | None = None) -> Iterator["DecisionLedger"]:
        """In this block, this thread's writes wait at most ``busy_timeout_ms`` for another writer.

        ``until`` (a ``time.perf_counter()`` value) also caps every wait at the time
        left.  Used for hook decisions, which must answer within their deadline
        even while another process holds the write lock.  Other threads are
        unaffected; nested blocks restore the outer bound.
        """
        previous = (getattr(self._local, "busy_ms", None), getattr(self._local, "until", None))
        self._local.busy_ms = max(0, int(busy_timeout_ms))
        self._local.until = until
        try:
            yield self
        finally:
            self._local.busy_ms, self._local.until = previous

    def _effective_busy_ms(self) -> int:
        bound = getattr(self._local, "busy_ms", None)
        if bound is None:
            return self.busy_timeout_ms
        until = getattr(self._local, "until", None)
        if until is not None:
            bound = min(bound, int((until - time.perf_counter()) * 1000))
        return max(0, bound)

    def _retry_locked(self, work: Callable[[], Any]) -> Any:
        """Run ``work``; in a bounded block, retry it on a lock error until the bound expires.

        Outside a bounded block it runs once (``open_conn`` and the connection's own
        ``busy_timeout`` do the waiting there).
        """
        if getattr(self._local, "busy_ms", None) is None:
            return work()
        end = time.perf_counter() + self._effective_busy_ms() / 1000.0
        while True:
            try:
                return work()
            except sqlite3.OperationalError as exc:
                left = end - time.perf_counter()
                if not _is_locked(exc) or left <= 0:
                    raise
                time.sleep(min(_BOUNDED_POLL_S, left))

    def _begin(self, conn: sqlite3.Connection) -> None:
        """``BEGIN IMMEDIATE``; in a bounded block, retried precisely until the bound expires."""
        self._retry_locked(lambda: conn.execute("BEGIN IMMEDIATE"))

    # ------------------------------------------------------------ plumbing ---
    def _record_failure(self, operation: str, exc: BaseException) -> None:
        global _failures, _logged
        self.write_failures += 1
        with _failure_lock:
            _failures += 1
            first = not _logged
            _logged = True
        if first:
            _log.warning("decision ledger %s failed (%s); further failures are counted silently",
                         operation, type(exc).__name__)

    def _connect(self) -> sqlite3.Connection:
        """Open through the fleet envelope (``open_conn``: WAL, busy_timeout, retry)."""
        if time.monotonic() < self._unavailable_until:
            raise LedgerUnavailable("ledger recently unavailable")
        try:
            # Persistent misconfigurations fail fast instead of through retries.
            if self.path.is_dir():
                raise LedgerUnavailable("ledger path is a directory")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and not os.access(self.path, os.R_OK | os.W_OK):
                raise LedgerUnavailable("ledger file is not writable")
            if getattr(self._local, "busy_ms", None) is None:
                conn = open_conn(self.path, busy_ms=self.busy_timeout_ms)
            else:  # "PRAGMA journal_mode = WAL" can meet another process's lock
                conn = self._retry_locked(lambda: _open_bounded(self.path))
        except Exception as exc:
            self._cool_down(exc)
            raise
        try:
            conn.isolation_level = None  # explicit BEGIN IMMEDIATE / COMMIT below
            conn.execute("PRAGMA synchronous = NORMAL")
            if not self._ready:
                self._ensure_schema(conn)
            elif self._retry_locked(lambda: conn.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
                raise LedgerUnavailable("schema version changed")
            return conn
        except BaseException as exc:
            conn.close()
            self._cool_down(exc)
            raise

    def _cool_down(self, exc: BaseException) -> None:
        """Fail fast for a while after an open failure; another writer's lock in a
        bounded (hook) block is momentary, not a reason to stop using the ledger."""
        if getattr(self._local, "busy_ms", None) is not None and _is_locked(exc):
            return
        self._unavailable_until = time.monotonic() + UNAVAILABLE_COOLDOWN_S

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version = self._retry_locked(lambda: conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise LedgerUnavailable("ledger schema is newer than this code")
        if version == SCHEMA_VERSION:
            present = self._retry_locked(lambda: {row[0] for row in conn.execute("SELECT name FROM sqlite_master")})
            if _SCHEMA_OBJECTS <= present:  # current: no write lock needed
                self._ready = True
                return
        self._begin(conn)
        try:
            for statement in _SCHEMA:
                conn.execute(statement)
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        self._ready = True

    def _write(self, operation: str, work) -> Any:
        try:
            conn = self._connect()
        except Exception as exc:
            self._record_failure(operation, exc)
            return None
        try:
            self._begin(conn)
            try:
                result = work(conn)
                conn.execute("COMMIT")
                return result
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except Exception as exc:
            self._record_failure(operation, exc)
            return None
        finally:
            conn.close()

    def exists(self) -> bool:
        """Whether the ledger file exists; read helpers never create it (Jev off leaves no file)."""
        return self.path.is_file()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Read helper; ``[]`` for an absent ledger (never created) or no rows.

        Raises :class:`LedgerReadError` when an existing ledger cannot be read, so a
        watermarked joiner never mistakes a failed read for "nothing to join".
        """
        if not self.exists():
            return []
        rows = self._read(sql, params)
        if rows is None:
            raise LedgerReadError("decision ledger read failed")
        return rows

    def _read(self, sql: str, params: Sequence[Any] = (), *,
              operation: str | None = None) -> list[dict[str, Any]] | None:
        """Rows, or ``None`` when the read failed; with ``operation`` a failure is also
        counted in ``ledger_write_failures`` (reads the engine needs before it may send)."""
        try:
            conn = self._connect()
        except Exception as exc:
            if operation:
                self._record_failure(operation, exc)
            return None
        try:
            return self._retry_locked(lambda: [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()])
        except Exception as exc:
            if operation:
                self._record_failure(operation, exc)
            return None
        finally:
            conn.close()

    # --------------------------------------------------------------- writes ---
    def write_decision(self, decision: DecisionRecord, items: Iterable[ItemRecord] = ()) -> bool:
        item_rows = [astuple(item) for item in items]

        def work(conn: sqlite3.Connection) -> bool:
            conn.execute(
                f"INSERT INTO decisions ({', '.join(DECISION_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in DECISION_COLUMNS)})",
                astuple(decision),
            )
            if item_rows:
                conn.executemany(
                    f"INSERT INTO decision_items ({', '.join(ITEM_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in ITEM_COLUMNS)})",
                    item_rows,
                )
            return True

        return bool(self._write("decision write", work))

    def reserve_send(self, decision_id: str, surface: str, *, ts: str, est_cost_usd: float) -> bool:
        """Durably record that a request is about to leave; ``False`` means do not send.

        The engine calls this after every pre-send check: a ledger that cannot take
        this write cannot take the decision row either, so nothing is sent unrecorded.
        """

        def work(conn: sqlite3.Connection) -> bool:
            conn.execute("INSERT INTO send_intents (decision_id, surface, ts, est_cost_usd) VALUES (?, ?, ?, ?)",
                         (decision_id, surface, ts, float(est_cost_usd)))
            return True

        return bool(self._write("send intent", work))

    def write_items(self, items: Iterable[ItemRecord]) -> bool:
        """Append item rows to an existing decision (e.g. answers that arrived late)."""
        rows = [astuple(item) for item in items]
        if not rows:
            return True

        def work(conn: sqlite3.Connection) -> bool:
            conn.executemany(
                f"INSERT OR IGNORE INTO decision_items ({', '.join(ITEM_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in ITEM_COLUMNS)})",
                rows,
            )
            return True

        return bool(self._write("item write", work))

    def record_outcomes(self, outcomes: Iterable[OutcomeRecord]) -> int:
        return self.record_outcomes_checked(outcomes) or 0

    def record_outcomes_checked(self, outcomes: Iterable[OutcomeRecord]) -> int | None:
        """Rows inserted, or ``None`` when the write failed (watermarked joiners must not advance)."""
        rows = [astuple(outcome) for outcome in outcomes]
        if not rows:
            return 0

        def work(conn: sqlite3.Connection) -> int:
            before = conn.total_changes
            conn.executemany(
                f"INSERT OR IGNORE INTO outcomes ({', '.join(OUTCOME_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in OUTCOME_COLUMNS)})",
                rows,
            )
            return conn.total_changes - before

        written = self._write("outcome write", work)
        return None if written is None else int(written)

    def register_questions(self, specs: Iterable[QuestionSpec]) -> bool:
        """Insert missing question versions; ``True`` when every one is stored.

        Read first: each hook process registers its questions, and versions that are
        already stored need no write transaction (no contention with other writers).
        """
        specs = list(specs)
        rows = [
            (spec.id, spec.version, spec.sha256, spec.primitive,
             json.dumps(spec.canonical(), sort_keys=True, ensure_ascii=False),
             json.dumps(default_thresholds(spec), sort_keys=True), utc_iso())
            for spec in specs
        ]
        if not rows:
            return True
        if self.exists():
            wanted = {(spec.id, spec.version) for spec in specs}
            placeholders = " OR ".join("(question_id = ? AND version = ?)" for _ in wanted)
            stored = self._read(f"SELECT question_id, version FROM question_versions WHERE {placeholders}",
                                [value for key in sorted(wanted) for value in key])
            if stored is not None and wanted <= {(row["question_id"], row["version"]) for row in stored}:
                return True

        def work(conn: sqlite3.Connection) -> bool:
            conn.executemany(
                "INSERT OR IGNORE INTO question_versions "
                "(question_id, version, sha256, primitive, text_json, threshold_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            return True

        return bool(self._write("question registration", work))

    def set_watermark(self, name: str, value: str) -> bool:
        def work(conn: sqlite3.Connection) -> bool:
            conn.execute(
                "INSERT INTO watermarks (name, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (name, value, utc_iso()),
            )
            return True

        return bool(self._write("watermark write", work))

    def claim_breaker_probe(self, surface: str, *, now_iso: str, since_iso: str) -> bool | None:
        """Atomically claim ``surface``'s breaker probe (one write transaction, so processes agree).

        ``True`` only when no request was sent on ``surface`` since ``since_iso``
        and no probe was claimed since then; the claim is recorded as the
        ``breaker_probe:<surface>`` watermark.  ``None`` when the write failed.
        """
        name = f"breaker_probe:{surface}"

        def work(conn: sqlite3.Connection) -> bool:
            last = conn.execute(
                "SELECT ts FROM decisions WHERE surface = ? AND attempt_count > 0 ORDER BY ts DESC LIMIT 1",
                (surface,),
            ).fetchone()
            claimed = conn.execute("SELECT value FROM watermarks WHERE name = ?", (name,)).fetchone()
            if (last is not None and last[0] and last[0] >= since_iso) or (
                    claimed is not None and claimed[0] and claimed[0] >= since_iso):
                return False
            conn.execute(
                "INSERT INTO watermarks (name, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (name, now_iso, utc_iso()),
            )
            return True

        return self._write("breaker probe claim", work)

    def prune_state(self, retention_days: int, *, now: datetime | None = None) -> int:
        """Null ``state_redacted`` older than the window; hashes and features stay."""
        cutoff = utc_iso((now or datetime.now(timezone.utc)) - timedelta(days=max(int(retention_days), 1)))

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "UPDATE decisions SET state_redacted = NULL WHERE ts < ? AND state_redacted IS NOT NULL", (cutoff,)
            )
            return cursor.rowcount

        return int(self._write("prune", work) or 0)

    # ---------------------------------------------------------------- reads ---
    def get_watermark(self, name: str, *, strict: bool = False) -> str | None:
        """The watermark's value, or ``None`` when it (or the ledger file) does not exist.

        A failed read also returns ``None`` unless ``strict``, which raises
        :class:`LedgerReadError` instead (and counts the failure): the breaker must
        not read "cannot tell" as "no open-until mark".
        """
        if not self.exists():
            return None
        sql, params = "SELECT value FROM watermarks WHERE name = ?", (name,)
        if not strict:
            rows = self._read(sql, params)
        elif (rows := self._read(sql, params, operation="watermark read")) is None:
            raise LedgerReadError("watermark read failed")
        return rows[0]["value"] if rows else None

    def thresholds(self, question_id: str, version: int) -> dict[str, float] | None:
        rows = self._read(
            "SELECT threshold_json FROM question_versions WHERE question_id = ? AND version = ?",
            (question_id, version),
        )
        if not rows:
            return None
        try:
            value = json.loads(rows[0]["threshold_json"] or "{}")
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def thresholds_many(self, keys: Iterable[tuple[str, int]]) -> dict[tuple[str, int], dict[str, float]] | None:
        """Stored thresholds for several ``(question_id, version)`` pairs in one read."""
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return {}
        clause = " OR ".join("(question_id = ? AND version = ?)" for _ in wanted)
        rows = self._read(f"SELECT question_id, version, threshold_json FROM question_versions WHERE {clause}",
                          [value for pair in wanted for value in pair], operation="threshold read")
        if rows is None:
            return None
        found: dict[tuple[str, int], dict[str, float]] = {}
        for row in rows:
            try:
                value = json.loads(row["threshold_json"] or "{}")
            except ValueError:
                continue
            if isinstance(value, dict):
                found[(row["question_id"], int(row["version"]))] = value
        return found

    # Orphan intents (a request left, its decision row never landed) count as spend and
    # requests: the caps must see every request, not only the recorded decisions.
    _ORPHAN_INTENTS = ("FROM send_intents i WHERE i.ts >= ? AND NOT EXISTS "
                       "(SELECT 1 FROM decisions d WHERE d.decision_id = i.decision_id)")

    def spend_since(self, since_iso: str) -> float | None:
        rows = self._read(
            "SELECT (SELECT COALESCE(SUM(cost_usd), 0.0) FROM decisions WHERE ts >= ?) + "
            f"(SELECT COALESCE(SUM(i.est_cost_usd), 0.0) {self._ORPHAN_INTENTS}) AS spend",
            (since_iso, since_iso), operation="budget read")
        return float(rows[0]["spend"]) if rows is not None and rows else None

    def requests_since(self, since_iso: str) -> int | None:
        rows = self._read(
            "SELECT (SELECT COALESCE(SUM(attempt_count), 0) FROM decisions WHERE ts >= ? AND attempt_count > 0) + "
            f"(SELECT COUNT(*) {self._ORPHAN_INTENTS}) AS n",
            (since_iso, since_iso), operation="budget read",
        )
        return int(rows[0]["n"]) if rows is not None and rows else None

    def orphan_send_intents(self, since_iso: str) -> int | None:
        """Requests that left without a decision row (should stay 0; watched by metrics)."""
        rows = self._read(f"SELECT COUNT(*) AS n {self._ORPHAN_INTENTS}", (since_iso,))
        return int(rows[0]["n"]) if rows is not None and rows else None

    def requested_decisions(self, surface: str, until_iso: str, limit: int = 200) -> list[dict[str, Any]] | None:
        """Newest-first decisions that actually sent a request (``None`` if unreadable)."""
        return self._read(
            "SELECT decision_id, ts, transport_outcome FROM decisions "
            "WHERE surface = ? AND attempt_count > 0 AND ts <= ? ORDER BY ts DESC LIMIT ?",
            (surface, until_iso, int(limit)), operation="breaker read",
        )


def prune_job(config: Any = None, *, now: datetime | None = None) -> int:
    """Retention job: null ``state_redacted`` older than ``state_retention_days``.

    Does nothing (and creates nothing) when the ledger file does not exist yet.
    """
    from memorymaster.decisions.config import DecisionConfig

    effective = config or DecisionConfig.from_env()
    if not Path(effective.decisions_db).exists():
        return 0
    return DecisionLedger(effective.decisions_db).prune_state(effective.state_retention_days, now=now)


__all__ = [
    "DECISION_COLUMNS",
    "DecisionLedger",
    "DecisionRecord",
    "ITEM_COLUMNS",
    "ItemRecord",
    "LedgerReadError",
    "OUTCOME_COLUMNS",
    "OutcomeRecord",
    "SCHEMA_VERSION",
    "ledger_write_failures",
    "prune_job",
    "utc_iso",
]
