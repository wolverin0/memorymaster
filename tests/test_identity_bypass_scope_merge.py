"""Adversarial tuple-namespace tests for scope canonicalization."""
from __future__ import annotations

import sqlite3

from scripts.merge_scope_variants import _archive_confirmed_collisions


OLD_SCOPE = "project:identity:variant"
NEW_SCOPE = "project:identity"
OLD_TIME = "2026-01-01T00:00:00+00:00"
NEW_TIME = "2026-02-01T00:00:00+00:00"


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE claims (
            id INTEGER PRIMARY KEY,
            subject TEXT,
            predicate TEXT,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            visibility TEXT NOT NULL,
            source_agent TEXT,
            tenant_id TEXT,
            updated_at TEXT,
            archived_at TEXT
        )
        """
    )
    return conn


def _claim(
    conn: sqlite3.Connection,
    claim_id: int,
    *,
    scope: str,
    visibility: str,
    principal: str,
    tenant: str = "tenant-a",
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO claims (
            id, subject, predicate, scope, status, visibility,
            source_agent, tenant_id, updated_at
        ) VALUES (?, 'shared-subject', 'uses', ?, 'confirmed', ?, ?, ?, ?)
        """,
        (claim_id, scope, visibility, principal, tenant, updated_at),
    )


def _statuses(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT status FROM claims ORDER BY id")]


def test_scope_merge_does_not_archive_other_private_principal() -> None:
    conn = _connection()
    _claim(
        conn,
        1,
        scope=OLD_SCOPE,
        visibility="private",
        principal="alice",
        updated_at=OLD_TIME,
    )
    _claim(
        conn,
        2,
        scope=NEW_SCOPE,
        visibility="private",
        principal="bob",
        updated_at=NEW_TIME,
    )

    archived = _archive_confirmed_collisions(conn, OLD_SCOPE, NEW_SCOPE)

    assert archived == 0
    assert _statuses(conn) == ["confirmed", "confirmed"]


def test_scope_merge_nonpublic_tuple_uses_exact_visibility() -> None:
    conn = _connection()
    _claim(
        conn,
        1,
        scope=OLD_SCOPE,
        visibility="private",
        principal="alice",
        updated_at=OLD_TIME,
    )
    _claim(
        conn,
        2,
        scope=NEW_SCOPE,
        visibility="sensitive",
        principal="alice",
        updated_at=NEW_TIME,
    )

    archived = _archive_confirmed_collisions(conn, OLD_SCOPE, NEW_SCOPE)

    assert archived == 0
    assert _statuses(conn) == ["confirmed", "confirmed"]


def test_scope_merge_does_not_archive_public_tuple_from_other_tenant() -> None:
    conn = _connection()
    _claim(
        conn,
        1,
        scope=OLD_SCOPE,
        visibility="public",
        principal="alice",
        tenant="tenant-a",
        updated_at=OLD_TIME,
    )
    _claim(
        conn,
        2,
        scope=NEW_SCOPE,
        visibility="public",
        principal="bob",
        tenant="tenant-b",
        updated_at=NEW_TIME,
    )

    archived = _archive_confirmed_collisions(conn, OLD_SCOPE, NEW_SCOPE)

    assert archived == 0
    assert _statuses(conn) == ["confirmed", "confirmed"]


def test_scope_merge_archives_same_private_namespace_collision() -> None:
    conn = _connection()
    _claim(
        conn,
        1,
        scope=OLD_SCOPE,
        visibility="private",
        principal="alice",
        updated_at=OLD_TIME,
    )
    _claim(
        conn,
        2,
        scope=NEW_SCOPE,
        visibility="private",
        principal="alice",
        updated_at=NEW_TIME,
    )

    archived = _archive_confirmed_collisions(conn, OLD_SCOPE, NEW_SCOPE)

    assert archived == 1
    assert _statuses(conn) == ["archived", "confirmed"]


def test_scope_merge_public_tuple_remains_tenant_wide() -> None:
    conn = _connection()
    _claim(
        conn,
        1,
        scope=OLD_SCOPE,
        visibility="public",
        principal="alice",
        updated_at=OLD_TIME,
    )
    _claim(
        conn,
        2,
        scope=NEW_SCOPE,
        visibility="public",
        principal="bob",
        updated_at=NEW_TIME,
    )

    archived = _archive_confirmed_collisions(conn, OLD_SCOPE, NEW_SCOPE)

    assert archived == 1
    assert _statuses(conn) == ["archived", "confirmed"]
