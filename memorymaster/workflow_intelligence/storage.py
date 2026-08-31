"""Disposable SQLite event store for rebuildable workflow analytics."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from memorymaster.stores._storage_shared import open_conn

from .models import ActionRecord, FeedbackRecord, SessionRecord, TurnRecord


SCHEMA_VERSION = "memorymaster.workflow-intelligence.v1"
TABLES = {
    "source_files", "sessions", "episodes", "turns", "actions", "feedback",
    "outcome_signals", "policy_snapshots", "candidates", "candidate_supports",
    "reviews", "completion_receipts", "scan_runs",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_schema (
    version TEXT PRIMARY KEY,
    installed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL UNIQUE,
    path_hash TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    prefix_hash TEXT NOT NULL DEFAULT '',
    cursor_offset INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'indexed',
    last_error TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_sources_provider ON source_files(provider, source_kind);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    external_id TEXT NOT NULL,
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    session_kind TEXT NOT NULL,
    project_scope TEXT NOT NULL DEFAULT 'global',
    root_session_hash TEXT NOT NULL DEFAULT '',
    parent_session_hash TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    initial_request_excerpt TEXT NOT NULL DEFAULT '',
    task_category TEXT NOT NULL DEFAULT 'unknown',
    completion_state TEXT NOT NULL DEFAULT 'unknown',
    verification_tier TEXT NOT NULL DEFAULT 'none',
    deep_parsed INTEGER NOT NULL DEFAULT 0,
    classification_provider TEXT NOT NULL DEFAULT '',
    classification_model TEXT NOT NULL DEFAULT '',
    classification_prompt_hash TEXT NOT NULL DEFAULT '',
    classification_authoritative INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_sessions_scope ON sessions(project_scope, provider);
CREATE INDEX IF NOT EXISTS idx_workflow_sessions_kind ON sessions(session_kind, deep_parsed);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    category TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    summary_excerpt TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(session_id, ordinal)
);
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL DEFAULT '',
    byte_start INTEGER NOT NULL DEFAULT 0,
    byte_end INTEGER NOT NULL DEFAULT 0,
    is_a2a INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_workflow_turns_session ON turns(session_id, ordinal);
CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    command_family TEXT NOT NULL DEFAULT '',
    byte_start INTEGER NOT NULL DEFAULT 0,
    byte_end INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_workflow_actions_session ON actions(session_id, ordinal);
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    theme TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL,
    user_origin INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_workflow_feedback_theme ON feedback(theme, session_id);
CREATE TABLE IF NOT EXISTS outcome_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL DEFAULT '',
    signal TEXT NOT NULL,
    polarity TEXT NOT NULL,
    verification_tier TEXT NOT NULL DEFAULT 'none',
    excerpt TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0
);
CREATE TABLE IF NOT EXISTS policy_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(session_id, source_kind, source_hash)
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    destination TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'watch',
    scope TEXT NOT NULL,
    trigger_excerpt TEXT NOT NULL,
    action_excerpt TEXT NOT NULL,
    rationale_excerpt TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0,
    support_count INTEGER NOT NULL DEFAULT 0,
    project_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_supports (
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    feedback_id TEXT NOT NULL DEFAULT '',
    project_scope TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    PRIMARY KEY(candidate_id, session_id, feedback_id)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    decision TEXT NOT NULL,
    reviewer TEXT NOT NULL DEFAULT 'human',
    rationale_excerpt TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS completion_receipts (
    receipt_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    session_hash TEXT NOT NULL,
    mode TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    mutation_seen INTEGER NOT NULL,
    verification_tier TEXT NOT NULL,
    completion_claimed INTEGER NOT NULL,
    warning_codes_json TEXT NOT NULL,
    action_counts_json TEXT NOT NULL,
    review_label TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_workflow_receipts_gate ON completion_receipts(observed_at, provider, mode);
CREATE TABLE IF NOT EXISTS scan_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    deep_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    source_files INTEGER NOT NULL DEFAULT 0,
    sessions INTEGER NOT NULL DEFAULT 0,
    deep_sessions INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]'
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def workflow_db_path() -> Path:
    configured = os.environ.get("MEMORYMASTER_WORKFLOW_DB", "").strip()
    return Path(configured) if configured else Path.home() / ".memorymaster" / "workflow-intelligence.db"


class WorkflowStore:
    """Small repository around the rebuildable workflow sidecar."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else workflow_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = open_conn(self.db_path, busy_ms=15_000)
        self._defer_commits = False
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(_SCHEMA)
        self._ensure_sidecar_columns()
        self._ensure_fts()
        self.connection.execute(
            "INSERT OR IGNORE INTO workflow_schema(version, installed_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utc_now()),
        )
        self.connection.commit()

    def _ensure_sidecar_columns(self) -> None:
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(completion_receipts)")
        }
        for name in ("review_label", "reviewed_at"):
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE completion_receipts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                )

    def _ensure_fts(self) -> None:
        try:
            self.connection.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                    turn_id UNINDEXED, excerpt, content='turns', content_rowid='rowid'
                );
                CREATE TRIGGER IF NOT EXISTS turns_fts_insert AFTER INSERT ON turns BEGIN
                    INSERT INTO turns_fts(rowid,turn_id,excerpt) VALUES(new.rowid,new.turn_id,new.excerpt);
                END;
                CREATE TRIGGER IF NOT EXISTS turns_fts_delete AFTER DELETE ON turns BEGIN
                    INSERT INTO turns_fts(turns_fts,rowid,turn_id,excerpt)
                    VALUES('delete',old.rowid,old.turn_id,old.excerpt);
                END;
                CREATE TRIGGER IF NOT EXISTS turns_fts_update AFTER UPDATE ON turns BEGIN
                    INSERT INTO turns_fts(turns_fts,rowid,turn_id,excerpt)
                    VALUES('delete',old.rowid,old.turn_id,old.excerpt);
                    INSERT INTO turns_fts(rowid,turn_id,excerpt) VALUES(new.rowid,new.turn_id,new.excerpt);
                END;
                """
            )
        except sqlite3.OperationalError:
            # Minimal SQLite builds may omit FTS5; the normalized store remains usable.
            return

    def close(self) -> None:
        self.connection.close()

    def _commit(self) -> None:
        if not self._defer_commits:
            self.connection.commit()

    @contextmanager
    def batch(self):
        """Commit one source-census transaction instead of one per file."""
        nested = self._defer_commits
        if not nested:
            self.connection.execute("BEGIN")
        self._defer_commits = True
        try:
            yield
            if not nested:
                self.connection.commit()
        except Exception:
            if not nested:
                self.connection.rollback()
            raise
        finally:
            self._defer_commits = nested

    def rows(self, table: str) -> list[sqlite3.Row]:
        if table not in TABLES:
            raise ValueError("unsupported workflow table")
        return self.connection.execute(f"SELECT * FROM {table}").fetchall()

    def session_rows(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM sessions ORDER BY started_at, session_id"
        ).fetchall()

    def upsert_source(
        self, *, path: Path, source_kind: str, provider: str, size: int,
        mtime_ns: int, prefix_hash: str, cursor_offset: int = 0,
        status: str = "indexed", last_error: str = "",
    ) -> int:
        import hashlib

        now = utc_now()
        resolved = str(path.absolute())
        path_hash = hashlib.sha256(resolved.encode("utf-8", errors="replace")).hexdigest()
        self.connection.execute(
            """INSERT INTO source_files(
                   source_path,path_hash,source_kind,provider,size_bytes,mtime_ns,
                   prefix_hash,cursor_offset,status,last_error,discovered_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_path) DO UPDATE SET
                   path_hash=excluded.path_hash, source_kind=excluded.source_kind,
                   provider=excluded.provider, size_bytes=excluded.size_bytes,
                   mtime_ns=excluded.mtime_ns, prefix_hash=excluded.prefix_hash,
                   cursor_offset=excluded.cursor_offset, status=excluded.status,
                   last_error=excluded.last_error, updated_at=excluded.updated_at""",
            (resolved, path_hash, source_kind, provider, size, mtime_ns, prefix_hash,
             cursor_offset, status, last_error, now, now),
        )
        row = self.connection.execute(
            "SELECT id FROM source_files WHERE source_path=?", (resolved,)
        ).fetchone()
        self._commit()
        return int(row[0])

    def source_row(self, path: Path) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM source_files WHERE source_path=?", (str(path.absolute()),)
        ).fetchone()

    def upsert_session(self, record: SessionRecord, *, source_file_id: int | None) -> None:
        values = (
            record.session_id, record.external_id, source_file_id, record.provider,
            record.session_kind, record.project_scope, record.root_session_hash,
            record.parent_session_hash, record.model, record.branch, record.worktree,
            record.started_at, record.ended_at, record.initial_request_excerpt,
            record.task_category, int(record.deep_parsed), utc_now(),
        )
        self.connection.execute(
            """INSERT INTO sessions(
                   session_id,external_id,source_file_id,provider,session_kind,
                   project_scope,root_session_hash,parent_session_hash,model,branch,
                   worktree,started_at,ended_at,initial_request_excerpt,task_category,
                   deep_parsed,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                   external_id=excluded.external_id,
                   source_file_id=COALESCE(excluded.source_file_id,sessions.source_file_id),
                   provider=excluded.provider, session_kind=excluded.session_kind,
                   project_scope=CASE WHEN excluded.project_scope='global'
                                      THEN sessions.project_scope ELSE excluded.project_scope END,
                   root_session_hash=CASE WHEN excluded.root_session_hash=''
                                          THEN sessions.root_session_hash ELSE excluded.root_session_hash END,
                   parent_session_hash=CASE WHEN excluded.parent_session_hash=''
                                            THEN sessions.parent_session_hash ELSE excluded.parent_session_hash END,
                   model=CASE WHEN excluded.model='' THEN sessions.model ELSE excluded.model END,
                   branch=CASE WHEN excluded.branch='' THEN sessions.branch ELSE excluded.branch END,
                   worktree=CASE WHEN excluded.worktree='' THEN sessions.worktree ELSE excluded.worktree END,
                   started_at=CASE WHEN sessions.started_at='' THEN excluded.started_at ELSE sessions.started_at END,
                   ended_at=CASE WHEN excluded.ended_at='' THEN sessions.ended_at ELSE excluded.ended_at END,
                   initial_request_excerpt=CASE WHEN excluded.initial_request_excerpt=''
                                                THEN sessions.initial_request_excerpt ELSE excluded.initial_request_excerpt END,
                   task_category=CASE WHEN excluded.task_category='unknown'
                                      THEN sessions.task_category ELSE excluded.task_category END,
                   deep_parsed=MAX(sessions.deep_parsed,excluded.deep_parsed), updated_at=excluded.updated_at""",
            values,
        )
        self._commit()

    def replace_details(
        self, session_id: str, turns: Iterable[TurnRecord],
        actions: Iterable[ActionRecord], feedback: Iterable[FeedbackRecord],
    ) -> None:
        turn_rows = list(turns)
        action_rows = list(actions)
        feedback_rows = list(feedback)
        for table in ("outcome_signals", "feedback", "actions", "turns", "episodes"):
            self.connection.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
        user_turns = [turn for turn in turn_rows if turn.role == "user" and not turn.is_a2a]
        self.connection.executemany(
            """INSERT INTO episodes(
                   session_id,ordinal,category,started_at,ended_at,summary_excerpt,metadata_json
               ) VALUES (?,?,?,?,?,?,?)""",
            [
                (session_id, ordinal, "task", turn.timestamp, turn.timestamp, turn.excerpt, "{}")
                for ordinal, turn in enumerate(user_turns)
            ],
        )
        self.connection.executemany(
            """INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?)""",
            [(t.turn_id, t.session_id, t.ordinal, t.role, t.excerpt, t.timestamp,
              t.byte_start, t.byte_end, int(t.is_a2a)) for t in turn_rows],
        )
        self.connection.executemany(
            """INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(a.action_id, a.session_id, a.turn_id, a.ordinal, a.kind, a.name,
              a.status, a.command_family, a.byte_start, a.byte_end, a.metadata_json)
             for a in action_rows],
        )
        self.connection.executemany(
            """INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?)""",
            [(f.feedback_id, f.session_id, f.turn_id, f.kind, f.theme, f.excerpt,
              f.confidence, int(f.user_origin)) for f in feedback_rows],
        )
        self.connection.execute(
            "UPDATE sessions SET deep_parsed=1, updated_at=? WHERE session_id=?",
            (utc_now(), session_id),
        )
        self._commit()

    def update_analysis(self, session_id: str, result: dict[str, Any]) -> None:
        tier = str(result["verification_tier"])
        state = str(result["completion_state"])
        self.connection.execute(
            """UPDATE sessions SET completion_state=?, verification_tier=?,
                   metadata_json=?, updated_at=? WHERE session_id=?""",
            (state, tier, json.dumps(result, sort_keys=True), utc_now(), session_id),
        )
        self.connection.execute("DELETE FROM outcome_signals WHERE session_id=?", (session_id,))
        verified_states = {
            "locally_verified", "runtime_verified", "deployed", "externally_accepted",
        }
        completion_polarity = (
            "positive" if state in verified_states else "negative" if state == "blocked" else "neutral"
        )
        signals = [(session_id, "", f"completion:{state}", completion_polarity, tier, "", 1.0)]
        signals.extend(
            (session_id, "", f"flag:{flag}", "negative", tier, "", 1.0)
            for flag in result.get("flags", [])
        )
        if int(result.get("correction_count", 0)):
            signals.append((session_id, "", "user_correction", "negative", tier, "", 1.0))
        self.connection.executemany(
            """INSERT INTO outcome_signals(
                   session_id,turn_id,signal,polarity,verification_tier,excerpt,confidence
               ) VALUES (?,?,?,?,?,?,?)""",
            signals,
        )
        self._commit()

    def insert_receipt(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO completion_receipts(
                   receipt_id,provider,session_hash,mode,observed_at,mutation_seen,
                   verification_tier,completion_claimed,warning_codes_json,action_counts_json,
                   review_label,reviewed_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["receipt_id"], row["provider"], row["session_hash"], row["mode"],
             row["observed_at"], int(row["mutation_seen"]), row["verification_tier"],
             int(row["completion_claimed"]), json.dumps(row["warning_codes"]),
             json.dumps(row["action_counts"], sort_keys=True), "", ""),
        )
        self._commit()
