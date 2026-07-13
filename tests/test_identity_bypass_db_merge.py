"""Adversarial identity-namespace tests for the SQLite DB merge bridge."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from memorymaster.bridges.db_merge import merge_databases
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.stores.storage import SQLiteStore


TENANT = "tenant-merge-identity"
SCOPE = "project:merge-identity"
CITATIONS = [CitationInput(source="identity-bypass-red", locator="db-merge")]


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.init_db()
    return store


def _claim(
    store: SQLiteStore,
    *,
    text: str,
    key: str,
    principal: str,
    visibility: str,
    object_value: str = "shared-value",
):
    return store.create_claim(
        text,
        CITATIONS,
        idempotency_key=key,
        subject="shared-subject",
        predicate="uses",
        object_value=object_value,
        scope=SCOPE,
        tenant_id=TENANT,
        source_agent=principal,
        visibility=visibility,
    )


def _rows(store: SQLiteStore) -> list[dict[str, object]]:
    with store.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM claims ORDER BY id")]


def test_merge_does_not_dedup_private_key_owned_by_another_principal(
    tmp_path: Path,
) -> None:
    """A hidden Alice key cannot be a uniqueness oracle that drops Bob's row."""
    target_path = tmp_path / "target-private-key.db"
    source_path = tmp_path / "source-private-key.db"
    target = _store(target_path)
    source = _store(source_path)
    _claim(
        target,
        text="Alice private merge payload.",
        key="shared-private-merge-key",
        principal="alice",
        visibility="private",
    )
    _claim(
        source,
        text="Bob private merge payload.",
        key="shared-private-merge-key",
        principal="bob",
        visibility="private",
    )

    stats = merge_databases(str(target_path), str(source_path))

    rows = _rows(target)
    assert stats["merged"] == 1
    assert {(row["source_agent"], row["visibility"]) for row in rows} == {
        ("alice", "private"),
        ("bob", "private"),
    }


def test_merge_text_hash_fallback_is_private_principal_local(tmp_path: Path) -> None:
    """Equal private text owned by different principals is not a duplicate."""
    target_path = tmp_path / "target-private-hash.db"
    source_path = tmp_path / "source-private-hash.db"
    target = _store(target_path)
    source = _store(source_path)
    _claim(
        target,
        text="The private deployment uses the same cache.",
        key="alice-private-hash-key",
        principal="alice",
        visibility="private",
    )
    _claim(
        source,
        text="  THE PRIVATE DEPLOYMENT USES THE SAME CACHE.  ",
        key="bob-private-hash-key",
        principal="bob",
        visibility="private",
    )

    stats = merge_databases(str(target_path), str(source_path))

    assert stats["merged"] == 1
    assert {row["source_agent"] for row in _rows(target)} == {"alice", "bob"}


def test_merge_conflict_resolution_does_not_supersede_foreign_private_tuple(
    tmp_path: Path,
) -> None:
    """A confirmed tuple only conflicts inside its exact private namespace."""
    target_path = tmp_path / "target-private-tuple.db"
    source_path = tmp_path / "source-private-tuple.db"
    target = _store(target_path)
    source = _store(source_path)
    alice = _claim(
        target,
        text="Alice private tuple value.",
        key="alice-private-tuple",
        principal="alice",
        visibility="private",
        object_value="alice-value",
    )
    bob = _claim(
        source,
        text="Bob private tuple value.",
        key="bob-private-tuple",
        principal="bob",
        visibility="private",
        object_value="bob-value",
    )
    transition_claim(
        target,
        alice.id,
        "confirmed",
        reason="target fixture",
        event_type="validator",
    )
    transition_claim(
        source,
        bob.id,
        "confirmed",
        reason="source fixture",
        event_type="validator",
    )
    with source.connect() as conn:
        conn.execute(
            "UPDATE claims SET updated_at='2099-01-01T00:00:00+00:00' WHERE id=?",
            (bob.id,),
        )

    stats = merge_databases(str(target_path), str(source_path))

    rows = _rows(target)
    assert stats["merged"] == 1
    assert {(row["source_agent"], row["status"]) for row in rows} == {
        ("alice", "confirmed"),
        ("bob", "confirmed"),
    }
    assert all(row["replaced_by_claim_id"] is None for row in rows)


def test_merge_public_key_remains_tenant_wide_across_principals(
    tmp_path: Path,
) -> None:
    """Public identities deliberately ignore source principal within a tenant."""
    target_path = tmp_path / "target-public-key.db"
    source_path = tmp_path / "source-public-key.db"
    target = _store(target_path)
    source = _store(source_path)
    _claim(
        target,
        text="Tenant-wide public merge identity.",
        key="public-merge-key",
        principal="alice",
        visibility="public",
    )
    _claim(
        source,
        text="Changed public payload still deduplicates.",
        key="public-merge-key",
        principal="bob",
        visibility="public",
    )

    stats = merge_databases(str(target_path), str(source_path))

    assert stats["skipped"] == 1
    assert len(_rows(target)) == 1


def test_legacy_source_rerun_uses_target_default_identity_namespace(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "modern-target.db"
    source_path = tmp_path / "legacy-source.db"
    target = _store(target_path)
    with sqlite3.connect(source_path) as conn:
        conn.execute(
            """
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                idempotency_key TEXT,
                status TEXT NOT NULL DEFAULT 'candidate',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO claims (
                text, idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                "Legacy source with target defaults.",
                "legacy-default-key",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    first = merge_databases(str(target_path), str(source_path))
    second = merge_databases(str(target_path), str(source_path))

    assert first == {"scanned": 1, "merged": 1, "skipped": 0, "errors": 0}
    assert second == {"scanned": 1, "merged": 0, "skipped": 1, "errors": 0}
    row = _rows(target)[0]
    assert row["scope"] == "project"
    assert row["visibility"] == "public"
