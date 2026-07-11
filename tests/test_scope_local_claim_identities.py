"""Adversarial contracts for scope-local claim identity namespaces.

An authenticated principal may be allowed to write more than one project
scope.  Identity keys therefore include the exact claim scope: otherwise a
row hidden by the scope RLS predicate can still become a uniqueness oracle.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.stores.postgres_store import PostgresStore
from memorymaster.stores.storage import generate_top_level_human_id


TENANT = "tenant-scope-identities"
SCOPE_A = "project:scope-a"
SCOPE_B = "project:scope-b"
CITATIONS = [CitationInput(source="scope-identity-red", locator="fixture")]
SCOPE_QUALIFIED_INDEXES = {
    "idx_claims_public_idempotency_key_unique",
    "idx_claims_nonpublic_principal_idempotency_key_unique",
    "idx_claims_public_human_id_unique",
    "idx_claims_nonpublic_principal_human_id_unique",
    "idx_claims_public_confirmed_tuple_unique",
    "idx_claims_nonpublic_principal_confirmed_tuple_unique",
}


def _bootstrap(db_path: Path) -> None:
    MemoryService(db_path, workspace_root=db_path.parent).init_db()


def _service(db_path: Path, principal: str, *allowed_scopes: str) -> MemoryService:
    return MemoryService(
        db_path,
        workspace_root=db_path.parent,
        tenant_id=TENANT,
        require_tenant=True,
        principal=principal,
        allowed_scopes=set(allowed_scopes),
    )


def _ingest(
    service: MemoryService,
    *,
    scope: str,
    key: str,
    visibility: str,
    text: str = "The same human-readable identity seed.",
):
    return service.ingest(
        text=text,
        citations=CITATIONS,
        idempotency_key=key,
        subject="scope-identity",
        predicate="uses",
        object_value="shared-value",
        scope=scope,
        visibility=visibility,
    )


def test_private_idempotency_key_is_independent_across_allowed_scopes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "private-key-scopes.db"
    _bootstrap(db_path)
    scope_a = _service(db_path, "alice", SCOPE_A)
    scope_b = _service(db_path, "alice", SCOPE_B)

    first = _ingest(
        scope_a,
        scope=SCOPE_A,
        key="private-same-key",
        visibility="private",
    )
    second = _ingest(
        scope_b,
        scope=SCOPE_B,
        key="private-same-key",
        visibility="private",
    )

    assert second.id != first.id
    assert second.scope == SCOPE_B
    assert first.scope == SCOPE_A
    assert second.human_id == first.human_id


def test_private_human_id_seed_is_independent_across_allowed_scopes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "private-human-scopes.db"
    _bootstrap(db_path)

    first = _ingest(
        _service(db_path, "alice", SCOPE_A),
        scope=SCOPE_A,
        key="private-human-a",
        visibility="private",
    )
    second = _ingest(
        _service(db_path, "alice", SCOPE_B),
        scope=SCOPE_B,
        key="private-human-b",
        visibility="private",
    )

    assert second.id != first.id
    assert second.human_id == first.human_id
    assert "~" not in second.human_id


def test_public_idempotency_key_does_not_oracle_an_inaccessible_scope(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "public-key-scopes.db"
    _bootstrap(db_path)

    first = _ingest(
        _service(db_path, "alice", SCOPE_A),
        scope=SCOPE_A,
        key="public-same-key",
        visibility="public",
    )
    second = _ingest(
        _service(db_path, "bob", SCOPE_B),
        scope=SCOPE_B,
        key="public-same-key",
        visibility="public",
    )

    assert second.id != first.id
    assert second.scope == SCOPE_B
    assert second.human_id == first.human_id


def test_public_human_id_seed_does_not_oracle_an_inaccessible_scope(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "public-human-scopes.db"
    _bootstrap(db_path)

    first = _ingest(
        _service(db_path, "alice", SCOPE_A),
        scope=SCOPE_A,
        key="public-human-a",
        visibility="public",
    )
    second = _ingest(
        _service(db_path, "bob", SCOPE_B),
        scope=SCOPE_B,
        key="public-human-b",
        visibility="public",
    )

    assert second.id != first.id
    assert second.human_id == first.human_id
    assert "~" not in second.human_id


def test_same_scope_public_identity_remains_shared_across_principals(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "same-scope-public.db"
    _bootstrap(db_path)

    first = _ingest(
        _service(db_path, "alice", SCOPE_A),
        scope=SCOPE_A,
        key="same-scope-public-key",
        visibility="public",
    )
    duplicate = _ingest(
        _service(db_path, "bob", SCOPE_A),
        scope=SCOPE_A,
        key="same-scope-public-key",
        visibility="public",
        text="A changed payload must still deduplicate in the same scope.",
    )

    assert duplicate.id == first.id


def test_same_scope_private_identity_remains_principal_local_and_deduplicated(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "same-scope-private.db"
    _bootstrap(db_path)
    alice = _service(db_path, "alice", SCOPE_A)

    first = _ingest(
        alice,
        scope=SCOPE_A,
        key="same-scope-private-key",
        visibility="private",
    )
    duplicate = _ingest(
        alice,
        scope=SCOPE_A,
        key="same-scope-private-key",
        visibility="private",
        text="A changed private payload still deduplicates for Alice.",
    )

    assert duplicate.id == first.id


def _canonical(sql: str) -> str:
    return " ".join(sql.lower().replace('"', "").split())


def _index_key_sql(sql: str) -> str:
    canonical = _canonical(sql)
    return canonical.split(" where ", 1)[0]


def test_postgres_runtime_catalog_scope_qualifies_all_six_identity_indexes() -> None:
    catalog = PostgresStore._expected_claim_identity_catalog()

    assert set(catalog) == SCOPE_QUALIFIED_INDEXES
    for name, (definition, _predicate) in catalog.items():
        key_sql = _index_key_sql(definition)
        assert re.search(r"\bscope\b", key_sql), name


def test_v0012_sqlite_and_postgres_migration_scope_qualify_all_six_indexes() -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "memorymaster"
        / "stores"
        / "migrations"
        / "0012_principal_local_claim_identities.py"
    )
    source = migration_path.read_text(encoding="utf-8")

    for name in SCOPE_QUALIFIED_INDEXES:
        matches = re.findall(
            rf"CREATE UNIQUE INDEX IF NOT EXISTS {name}\s+ON claims\((.*?)\)\s+WHERE",
            source,
            flags=re.IGNORECASE | re.DOTALL,
        )
        assert len(matches) == 2, name
        assert all(re.search(r"\bscope\b", match, re.IGNORECASE) for match in matches), name


class InvisibleCrossScopeHumanCursor:
    """Expose a collision unless allocator SQL binds the requested scope."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.row: dict[str, object] | None = None
        self.identity_probes = 0

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        canonical = _canonical(sql)
        bound = tuple(params)
        self.executed.append((canonical, bound))
        if "from claim_links" in canonical:
            self.row = None
            return
        if "select 1 from claims" in canonical:
            self.identity_probes += 1
            if self.identity_probes > 2:
                raise AssertionError("allocator looped on an invisible cross-scope human ID")
            is_scope_qualified = "scope = %s" in canonical and SCOPE_A in bound
            self.row = None if is_scope_qualified else {"exists": 1}
            return
        raise AssertionError(f"unexpected allocator SQL: {canonical}")

    def fetchone(self) -> dict[str, object] | None:
        return self.row


def test_postgres_human_id_allocator_ignores_invisible_cross_scope_collision() -> None:
    cursor = InvisibleCrossScopeHumanCursor()
    expected = generate_top_level_human_id(
        "scope-identity",
        "The same human-readable identity seed.",
    )

    allocated = PostgresStore._allocate_human_id(
        cursor,
        "scope-identity",
        "The same human-readable identity seed.",
        41,
        tenant_id=TENANT,
        scope=SCOPE_A,
        visibility="private",
        source_agent="alice",
    )

    assert allocated == expected
    assert cursor.identity_probes == 1
    assert all(
        "scope = %s" in sql and SCOPE_A in params
        for sql, params in cursor.executed
        if "claims" in sql
    )


class IdempotencyFallbackCursor:
    """Return the hidden row only when fallback SQL omits its scope boundary."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.row: dict[str, object] | None = None

    def __enter__(self) -> IdempotencyFallbackCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        canonical = _canonical(sql)
        bound = tuple(params)
        self.executed.append((canonical, bound))
        if canonical.startswith("insert into claims"):
            self.row = None
            return
        if canonical.startswith("select id from claims"):
            is_scope_qualified = "scope = %s" in canonical and SCOPE_A in bound
            self.row = None if is_scope_qualified else {"id": 999}
            return
        raise AssertionError(f"write followed unsafe fallback path: {canonical}")

    def fetchone(self) -> dict[str, object] | None:
        return self.row


class IdempotencyFallbackConnection:
    def __init__(self, cursor: IdempotencyFallbackCursor) -> None:
        self.cursor_instance = cursor

    def __enter__(self) -> IdempotencyFallbackConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> IdempotencyFallbackCursor:
        return self.cursor_instance


def test_postgres_idempotency_fallback_never_returns_hidden_cross_scope_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PostgresStore(
        "postgresql://runtime.invalid/memorymaster",
        tenant_id=TENANT,
        require_tenant=True,
        principal="alice",
        allowed_scopes={SCOPE_A},
    )
    cursor = IdempotencyFallbackCursor()
    monkeypatch.setattr(store, "connect", lambda: IdempotencyFallbackConnection(cursor))
    monkeypatch.setattr(
        store,
        "get_claim",
        lambda *_args, **_kwargs: pytest.fail("hidden cross-scope claim was resolved"),
    )

    with pytest.raises(RuntimeError, match="Idempotency key matched missing claim"):
        store.create_claim(
            "Cross-scope fallback payload.",
            CITATIONS,
            idempotency_key="cross-scope-fallback-key",
            subject="scope-identity",
            scope=SCOPE_A,
            tenant_id=TENANT,
            source_agent="alice",
            visibility="private",
        )

    fallback_sql, fallback_params = next(
        (sql, params)
        for sql, params in cursor.executed
        if sql.startswith("select id from claims")
    )
    assert "scope = %s" in fallback_sql
    assert SCOPE_A in fallback_params
