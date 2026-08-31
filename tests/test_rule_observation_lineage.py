from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

from memorymaster.knowledge.rule_observations import (
    observation_support,
    record_rule_observation,
)


def _connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0024_rule_observation_lineage"
    )
    migration.apply_sqlite(conn)
    return conn


def test_same_root_session_increments_activity_not_independent_support(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "main.db")
    try:
        for _ in range(2):
            record_rule_observation(
                conn,
                rule_fingerprint="rule-1",
                provider="claude",
                root_session_id="session-a",
                project_scope="project:demo",
                source_ref="claude:offset:10",
                evidence_hash="a" * 64,
            )
        support = observation_support(conn, "rule-1", scope="project:demo")
        row = conn.execute("SELECT * FROM rule_observations").fetchone()
        assert support["root_sessions"] == 1
        assert support["eligible"] is False
        assert row["event_count"] == 2
        assert row["root_session_hash"] != "session-a"
    finally:
        conn.close()


def test_project_and_global_eligibility_require_independent_roots(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "main.db")
    try:
        for session, project in [
            ("s1", "project:a"),
            ("s2", "project:a"),
            ("s3", "project:a"),
            ("s4", "project:b"),
        ]:
            record_rule_observation(
                conn,
                rule_fingerprint="rule-x",
                provider="codex",
                root_session_id=session,
                project_scope=project,
                source_ref=f"codex:{session}",
                evidence_hash="b" * 64,
            )
        project = observation_support(conn, "rule-x", scope="project:a")
        global_support = observation_support(conn, "rule-x", scope="user")
        assert project == {"root_sessions": 3, "projects": 1, "eligible": True}
        assert global_support == {"root_sessions": 4, "projects": 2, "eligible": True}
    finally:
        conn.close()


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params=None) -> None:
        self.statements.append(sql)


class _PostgresConnection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()
        self.committed = False

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.committed = True


def test_migration_has_postgres_parity() -> None:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0024_rule_observation_lineage"
    )
    conn = _PostgresConnection()
    migration.apply_postgres(conn)
    statements = "\n".join(conn.cursor_instance.statements)
    assert "CREATE TABLE IF NOT EXISTS rule_observations" in statements
    assert "root_session_hash" in statements
    assert conn.committed is True

