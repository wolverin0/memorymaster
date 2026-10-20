"""The scope census is explicit, narrow and read-only on disposable SQLite."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from scripts import census_single_use_scopes as census


@pytest.mark.parametrize(
    "scope",
    [
        "project:t1661", "project:t-1661", "project:t-card", "project:infra-pr42",
        "project:memorymaster-pr123", "project:memorymaster-main-wt", "project:memorymaster_wt_t1661",
        "project:worktree-feature", "project:worktrees_t1661", "project:_wt", "project:WT-test",
    ],
)
def test_explicit_single_use_patterns(scope):
    assert census.is_single_use_scope(scope)


@pytest.mark.parametrize(
    "scope",
    [
        "project:memorymaster", "project:infra", "project:py-apps", "project:twitter",
        "project:t1661-docs", "project:t-cardinal", "project:infra-pr",
        "project:infra-pr42-docs", "user", "global", "team:t1661", "user:t1661",
        "project:person:t1661", "project:C:/Users/private/_wt/t1661", "project:t1661\nsecret",
    ],
)
def test_durable_or_unrecognized_scopes_are_excluded(scope):
    assert not census.is_single_use_scope(scope)


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY, scope TEXT, text TEXT)")
        connection.executemany(
            "INSERT INTO claims VALUES (?, ?, ?)",
            [(1, "project:t1661", "PRIVATE CLAIM SECRET"),
             (2, "project:memorymaster", "OTHER PRIVATE TEXT"),
             (3, "project:memorymaster-pr42", "PRIVATE CLAIM SECRET")],
        )


def test_cli_reports_only_ids_and_scopes_and_preserves_database(tmp_path):
    database = tmp_path / "private-name # with spaces.sqlite"
    _database(database)
    before = database.read_bytes()
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.census_single_use_scopes", "--dry-run", "--db", str(database)],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert [json.loads(line) for line in completed.stdout.splitlines()] == [
        {"claim_id": 1, "current_scope": "project:t1661"},
        {"claim_id": 3, "current_scope": "project:memorymaster-pr42"},
    ]
    assert completed.stderr == ""
    assert "PRIVATE" not in completed.stdout
    assert str(tmp_path) not in completed.stdout
    assert database.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [database.name]


def test_connection_is_read_only_and_query_only_and_never_reads_claim_text(tmp_path, monkeypatch):
    database = tmp_path / "fixture.sqlite"
    _database(database)
    connect = sqlite3.connect
    checked = []

    class GuardedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("SELECT"):
                assert super().execute("PRAGMA query_only").fetchone() == (1,)
                for statement in ("INSERT INTO claims VALUES (4, 'global', 'x')", "PRAGMA user_version = 9"):
                    with pytest.raises(sqlite3.OperationalError, match="readonly"):
                        super().execute(statement)
                checked.append(sql)
            return super().execute(sql, *args, **kwargs)

    def guarded_connect(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro")
        assert kwargs["uri"] is True
        connection = connect(database_uri, factory=GuardedConnection, **kwargs)

        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == "claims":
                assert column in {"id", "scope"}
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(census.sqlite3, "connect", guarded_connect)
    assert census.affected_claims(database) == [(1, "project:t1661"), (3, "project:memorymaster-pr42")]
    assert checked


@pytest.mark.parametrize("args", [[], ["--dry-run"], ["--db", "unopened.sqlite"]])
def test_cli_requires_explicit_database_and_dry_run(args, monkeypatch):
    def forbidden_connect(*_args, **_kwargs):
        pytest.fail("argument rejection must happen before any database opens")

    monkeypatch.setattr(census.sqlite3, "connect", forbidden_connect)
    with pytest.raises(SystemExit) as error:
        census.main(args)
    assert error.value.code == 2


@pytest.mark.parametrize("exists", [False, True])
def test_failure_is_sanitized_and_missing_database_is_not_created(tmp_path, capsys, exists):
    database = tmp_path / "private-location.sqlite"
    if exists:
        database.write_bytes(b"not sqlite PRIVATE SECRET")
    assert census.main(["--dry-run", "--db", str(database)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert str(tmp_path) not in output.err
    assert "PRIVATE" not in output.err
    assert database.exists() == exists
