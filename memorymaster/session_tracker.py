"""Agent session tracking backed by a lightweight SQLite table."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    session_start REAL NOT NULL,
    last_activity REAL NOT NULL,
    claims_ingested INTEGER NOT NULL DEFAULT 0,
    queries_made INTEGER NOT NULL DEFAULT 0
)
"""

_ACTIVE_WINDOW_SECONDS = 3600  # sessions idle >1 h are considered inactive


class SessionTracker:
    """Track agent sessions in a SQLite database."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._ensure_table()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)
            conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_session(self, agent_id: str) -> int:
        """Create a new session for *agent_id* and return the session_id.

        Each concurrent session from the same agent gets a unique session_id.
        """
        if not agent_id or not isinstance(agent_id, str):
            raise ValueError("agent_id must be a non-empty string")

        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO agent_sessions (agent_id, session_start, last_activity) VALUES (?, ?, ?)",
                (agent_id.strip(), now, now),
            )
            conn.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    def record_activity(self, session_id: int, activity_type: str) -> None:
        """Update *last_activity* and increment the matching counter.

        Validates session_id and activity_type before recording.
        """
        if not isinstance(session_id, int) or session_id <= 0:
            raise ValueError("session_id must be a positive integer")

        now = time.time()
        if activity_type == "ingest":
            sql = "UPDATE agent_sessions SET last_activity=?, claims_ingested=claims_ingested+1 WHERE id=?"
        elif activity_type == "query":
            sql = "UPDATE agent_sessions SET last_activity=?, queries_made=queries_made+1 WHERE id=?"
        else:
            sql = "UPDATE agent_sessions SET last_activity=? WHERE id=?"

        with self._connect() as conn:
            cursor = conn.execute(sql, (now, session_id))
            conn.commit()
            if cursor.rowcount == 0:
                # Session doesn't exist, silently skip (already closed or invalid)
                pass

    def get_active_sessions(self) -> list[dict]:
        """Return sessions with activity within the last hour.

        Returns empty list if no active sessions or DB is empty.
        """
        try:
            cutoff = time.time() - _ACTIVE_WINDOW_SECONDS
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM agent_sessions WHERE last_activity >= ? ORDER BY last_activity DESC",
                    (cutoff,),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            # Table doesn't exist yet (empty DB)
            return []

    def get_session_stats(self, agent_id: str) -> dict:
        """Return aggregate stats for *agent_id* across all sessions."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_sessions,
                    COALESCE(SUM(claims_ingested), 0) AS total_claims,
                    COALESCE(SUM(queries_made), 0) AS total_queries
                FROM agent_sessions
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            return {"agent_id": agent_id, "total_sessions": 0, "total_claims": 0, "total_queries": 0}
        return {
            "agent_id": agent_id,
            "total_sessions": row["total_sessions"],
            "total_claims": row["total_claims"],
            "total_queries": row["total_queries"],
        }
