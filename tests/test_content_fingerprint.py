"""T-1865 / F14: content dedup must not depend on the first copy's idempotency key.

The facade used to look identical content up in the ``idempotency_key`` column
(``hash-...``), so an original ingested under an explicit key was invisible to a later
unkeyed copy and duplicates 1, 2, 3... accumulated. Content now has its own
fingerprint column under the (tenant, scope, visibility, private owner) identity.
"""
from __future__ import annotations

import importlib
import sqlite3

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.stores.claim_identity import content_fingerprint
from memorymaster.stores.migrations import MigrationRunner
from memorymaster.stores.storage import SQLiteStore

migration = importlib.import_module("memorymaster.stores.migrations.0030_claim_content_fingerprint")

TEXT = "The nightly backup job rotates the encrypted archives after thirty days"
CITE = [CitationInput(source="test")]


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "fp.db"
    MemoryService(str(path)).init_db()
    return path


def _ingest(db, text=TEXT, **kwargs):
    tenant = kwargs.pop("tenant_id", None)
    return MemoryService(str(db), tenant_id=tenant).ingest(text=text, citations=CITE, **kwargs)


def test_unkeyed_copy_of_a_keyed_original_returns_the_original(db):
    first = _ingest(db, idempotency_key="producer-key-1")
    second = _ingest(db)
    third = _ingest(db)
    assert first.id == second.id == third.id
    assert len(SQLiteStore(db).list_claims(limit=50, include_archived=True)) == 1


def test_case_and_edge_whitespace_do_not_create_copies(db):
    first = _ingest(db, idempotency_key="k")
    assert _ingest(db, text=f"  {TEXT.upper()}  ").id == first.id


def test_distinct_identities_never_collapse(db):
    base = _ingest(db, idempotency_key="k0")
    other_scope = _ingest(db, scope="project:other")
    other_tenant = _ingest(db, tenant_id="t1")
    private_alice = _ingest(db, visibility="private", source_agent="alice")
    private_bob = _ingest(db, visibility="private", source_agent="bob")
    private_alice_again = _ingest(db, visibility="private", source_agent="alice")
    ids = {base.id, other_scope.id, other_tenant.id, private_alice.id, private_bob.id}
    assert len(ids) == 5
    assert private_alice_again.id == private_alice.id


def test_different_text_is_not_a_copy(db):
    assert _ingest(db, idempotency_key="k").id != _ingest(db, text=TEXT + " and then verifies them").id


def test_fingerprint_is_stored_on_create(db):
    claim = _ingest(db, idempotency_key="k")
    with sqlite3.connect(db) as conn:
        stored = conn.execute("SELECT content_fingerprint FROM claims WHERE id=?", (claim.id,)).fetchone()[0]
    assert stored == content_fingerprint(claim.text)


def test_store_lookup_prefers_a_live_claim_over_an_archived_one(db):
    store = SQLiteStore(db)
    fp = content_fingerprint(TEXT)
    archived = store.create_claim(TEXT, CITE, idempotency_key="a")
    with store.connect() as conn:
        conn.execute("UPDATE claims SET status='archived' WHERE id=?", (archived.id,))
        conn.commit()
    live = store.create_claim(TEXT, CITE, idempotency_key="b")
    found = store.get_claim_by_content_fingerprint(
        fp, tenant_id=None, scope="project", visibility="public", source_agent=None
    )
    assert found is not None and found.id == live.id


# --- migration 0030 -------------------------------------------------------------------

_LEGACY = """
CREATE TABLE claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, tenant_id TEXT,
    scope TEXT NOT NULL DEFAULT 'project', visibility TEXT NOT NULL DEFAULT 'public',
    source_agent TEXT, status TEXT NOT NULL DEFAULT 'candidate',
    updated_at TEXT NOT NULL DEFAULT '2020-01-01T00:00:00+00:00'
);
"""


def _legacy_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_LEGACY)
    conn.executemany(
        "INSERT INTO claims(text, tenant_id, scope) VALUES (?,?,?)",
        [("Alpha fact", None, "project:a"), ("  BETA fact ", "t1", "project:b"), ("Alpha fact", None, "project:c")],
    )
    conn.commit()
    return conn


def test_migration_backfills_fingerprints_without_touching_claim_data():
    conn = _legacy_db()
    before = conn.execute("SELECT id, text, status, updated_at FROM claims ORDER BY id").fetchall()
    migration.apply_sqlite(conn)
    rows = conn.execute("SELECT id, text, status, updated_at, content_fingerprint FROM claims ORDER BY id").fetchall()
    assert [r[:4] for r in rows] == before  # text, status and updated_at untouched
    assert [r[4] for r in rows] == [content_fingerprint(r[1]) for r in rows]
    assert rows[0][4] == rows[2][4] != rows[1][4]


def test_migration_is_retryable_and_idempotent():
    conn = _legacy_db()
    migration.apply_sqlite(conn)
    conn.execute("UPDATE claims SET content_fingerprint = NULL WHERE id = 2")  # simulated partial state
    conn.commit()
    migration.apply_sqlite(conn)
    migration.apply_sqlite(conn)
    assert conn.execute("SELECT COUNT(*) FROM claims WHERE content_fingerprint IS NULL").fetchone()[0] == 0


def test_migration_skips_rows_a_guard_trigger_rejects_instead_of_failing():
    conn = _legacy_db()
    conn.executescript(
        "CREATE TRIGGER guard BEFORE UPDATE ON claims WHEN NEW.text = 'Alpha fact' "
        "AND NEW.scope = 'project:c' BEGIN SELECT RAISE(ABORT, 'legacy row rejected'); END;"
    )
    migration.apply_sqlite(conn)
    nulls = conn.execute("SELECT id FROM claims WHERE content_fingerprint IS NULL").fetchall()
    assert nulls == [(3,)]  # left NULL: the old key-based lookup still covers it


def test_migration_is_sqlite_only_and_postgres_fails_closed():
    with pytest.raises(RuntimeError, match="migration 30 is SQLite-only"):
        migration.apply_postgres(object())


def test_runner_applies_0030_on_a_real_database(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        assert 30 in {r["version"] for r in conn.execute("SELECT version FROM schema_versions")}
        assert "content_fingerprint" in {r["name"] for r in conn.execute("PRAGMA table_info(claims)")}
        assert MigrationRunner(conn, backend="sqlite").apply_pending() == []
