"""PostgreSQL runtime RED contracts for v0012 claim identities."""
from __future__ import annotations

import copy
import inspect
import re
from types import SimpleNamespace
from typing import Sequence

import pytest
from psycopg.errors import UniqueViolation

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.stores._storage_write_claims import _WriteClaimsMixin
from memorymaster.stores.migrations import discover_migrations
from memorymaster.stores.postgres_store import PostgresStore

from test_principal_local_identity_migration import (
    IDENTITY_INDEXES,
    LEGACY_UNIQUE_INDEXES,
)
HUMAN_ID_INDEXES = {
    "idx_claims_public_human_id_unique",
    "idx_claims_nonpublic_principal_human_id_unique",
}


def _canonical(sql: str) -> str:
    return " ".join(sql.lower().replace('"', "").split())


def _index_row(name: str) -> dict[str, object]:
    public = "_public_" in name
    if "idempotency_key" in name:
        identity_columns = "scope, idempotency_key"
        nonnull = "idempotency_key IS NOT NULL"
    elif "human_id" in name:
        identity_columns = "scope, human_id"
        nonnull = "human_id IS NOT NULL"
    else:
        identity_columns = "subject, predicate, scope"
        nonnull = (
            "status = 'confirmed'::text AND subject IS NOT NULL "
            "AND predicate IS NOT NULL"
        )
    if public:
        columns = f"COALESCE(tenant_id, ''::text), {identity_columns}"
        predicate = f"visibility = 'public'::text AND {nonnull}"
    else:
        if "scope, " in identity_columns and "confirmed" not in name:
            columns = (
                "COALESCE(tenant_id, ''::text), scope, visibility, source_agent, "
                f"{identity_columns.removeprefix('scope, ')}"
            )
        else:
            columns = (
                "COALESCE(tenant_id, ''::text), visibility, source_agent, "
                f"{identity_columns}"
            )
        predicate = (
            "visibility <> 'public'::text AND source_agent IS NOT NULL "
            f"AND {nonnull}"
        )
    indexdef = (
        f"CREATE UNIQUE INDEX {name} ON public.claims USING btree ({columns}) "
        f"WHERE ({predicate})"
    )
    return {
        "index_name": name,
        "relname": name,
        "indisunique": True,
        "is_unique": True,
        "indisvalid": True,
        "is_valid": True,
        "indisready": True,
        "is_ready": True,
        "indexdef": indexdef,
        "index_definition": indexdef,
        "predicate": predicate,
        "index_predicate": predicate,
    }


class IndexCatalogCursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        self.executed.append((sql, tuple(params)))

    def fetchall(self) -> list[dict[str, object]]:
        return list(self.rows)

    def fetchone(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


def _safe_index_rows() -> list[dict[str, object]]:
    return [_index_row(name) for name in sorted(IDENTITY_INDEXES)]


def test_runtime_accepts_only_exact_six_principal_identity_indexes() -> None:
    cursor = IndexCatalogCursor(_safe_index_rows())

    PostgresStore._validate_claim_identity_indexes(cursor)

    query = _canonical(cursor.executed[0][0])
    assert "pg_index" in query
    assert "indisunique" in query
    assert "indisvalid" in query
    assert "indisready" in query
    assert "pg_get_indexdef" in query
    assert "pg_get_expr" in query


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "legacy",
        "not_unique",
        "not_valid",
        "not_ready",
        "wrong_columns",
        "wrong_predicate",
    ],
)
def test_runtime_rejects_identity_index_catalog_drift(mutation: str) -> None:
    rows = _safe_index_rows()
    if mutation == "missing":
        rows.pop()
    elif mutation == "extra":
        extra = copy.deepcopy(rows[0])
        extra["index_name"] = extra["relname"] = "idx_claims_extra_unique"
        rows.append(extra)
    elif mutation == "legacy":
        legacy = copy.deepcopy(rows[0])
        legacy["index_name"] = legacy["relname"] = next(iter(LEGACY_UNIQUE_INDEXES))
        rows.append(legacy)
    elif mutation == "wrong_columns":
        rows[0]["indexdef"] = rows[0]["index_definition"] = str(
            rows[0]["indexdef"]
        ).replace("source_agent", "holder")
    elif mutation == "wrong_predicate":
        rows[0]["predicate"] = rows[0]["index_predicate"] = "TRUE"
    else:
        field, alias = {
            "not_unique": ("indisunique", "is_unique"),
            "not_valid": ("indisvalid", "is_valid"),
            "not_ready": ("indisready", "is_ready"),
        }[mutation]
        rows[0][field] = False
        rows[0][alias] = False

    with pytest.raises(PermissionError, match="(?i)(identity|index|catalog|unique)"):
        PostgresStore._validate_claim_identity_indexes(IndexCatalogCursor(rows))


def test_runtime_preserves_literal_case_in_identity_index_fingerprint() -> None:
    rows = _safe_index_rows()
    public_row = next(row for row in rows if "_public_" in str(row["index_name"]))
    for field in ("indexdef", "index_definition", "predicate", "index_predicate"):
        public_row[field] = str(public_row[field]).replace("'public'", "'PUBLIC'")

    with pytest.raises(PermissionError, match="(?i)(identity|index|catalog|unique)"):
        PostgresStore._validate_claim_identity_indexes(IndexCatalogCursor(rows))


class RuntimeMigrationCursor:
    def __init__(self, checksums: dict[int, str]) -> None:
        self.checksums = checksums
        self.requested_versions: set[int] = set()
        self.rows: list[dict[str, object]] = []

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        versions = {int(value) for value in params if isinstance(value, int)}
        if not versions:
            versions = {
                int(value)
                for value in re.findall(r"\b(?:11|12)\b", sql)
            }
        self.requested_versions.update(versions)
        self.rows = [
            {"version": version, "checksum": self.checksums[version]}
            for version in sorted(versions)
            if version in self.checksums
        ]

    def fetchone(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, object]]:
        return list(self.rows)


def _migration_checksums() -> dict[int, str]:
    migrations = {item.version: item for item in discover_migrations()}
    return {version: migrations[version].checksum() for version in (11, 12)}


def test_runtime_requires_v0012_source_checksum() -> None:
    checksums = _migration_checksums()
    checksums[12] = "0" * 64
    cursor = RuntimeMigrationCursor(checksums)

    with pytest.raises(PermissionError, match="(?i)(migration|checksum|version)"):
        PostgresStore._validate_runtime_migration(cursor)

    assert 12 in cursor.requested_versions


def test_runtime_accepts_matching_v0011_and_v0012_checksums() -> None:
    cursor = RuntimeMigrationCursor(_migration_checksums())

    PostgresStore._validate_runtime_migration(cursor)

    assert cursor.requested_versions == {11, 12}


class BareCursor:
    def __enter__(self) -> BareCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class BareConnection:
    def __init__(self) -> None:
        self.autocommit = True
        self.closed = False

    def cursor(self) -> BareCursor:
        return BareCursor()

    def close(self) -> None:
        self.closed = True


def test_team_connect_validates_v12_identity_catalog_before_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PostgresStore(
        "postgresql://runtime.invalid/memorymaster",
        tenant_id="tenant-a",
        require_tenant=True,
        principal="alice",
        allowed_scopes={"project:a"},
    )
    connection = BareConnection()
    calls: list[str] = []
    monkeypatch.setattr(store, "_open_connection", lambda: connection)
    for method in (
        "_validate_runtime_role",
        "_validate_runtime_tables",
        "_validate_runtime_metadata_tables",
        "_validate_claim_owner_constraint",
        "_validate_confirmed_tuple_index",
        "_validate_claim_supersession_guard",
        "_validate_event_append_only_catalog",
        "_validate_event_chain_head_function",
        "_validate_runtime_migration",
        "_validate_runtime_policies",
    ):
        monkeypatch.setattr(
            PostgresStore,
            method,
            classmethod(lambda _cls, _cur: None),
        )
    monkeypatch.setattr(
        PostgresStore,
        "_validate_claim_identity_indexes",
        classmethod(lambda _cls, _cur: calls.append("identity")),
        raising=False,
    )
    monkeypatch.setattr(
        PostgresStore,
        "_bind_runtime_authority",
        classmethod(
            lambda _cls, _cur, _tenant, _principal, _scopes: calls.append("bind")
        ),
    )

    assert store.connect() is connection
    assert calls == ["identity", "bind"]


def test_postgres_create_rejects_sensitive_visibility_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PostgresStore(
        "postgresql://runtime.invalid/memorymaster",
        tenant_id="tenant-a",
        require_tenant=True,
        principal="alice",
        allowed_scopes={"project:a"},
    )
    monkeypatch.setattr(
        store,
        "connect",
        lambda: (_ for _ in ()).throw(
            AssertionError("database opened before sensitive visibility denial")
        ),
    )

    with pytest.raises((PermissionError, ValueError), match="(?i)(sensitive|visibility)"):
        store.create_claim(
            "Team-sensitive payload.",
            [CitationInput(source="identity-red", locator="fixture")],
            idempotency_key="team-sensitive-denied",
            scope="project:a",
            tenant_id="tenant-a",
            source_agent="alice",
            visibility="sensitive",
        )


class NamedUniqueViolation(UniqueViolation):
    def __init__(self, constraint_name: str) -> None:
        super().__init__(f"duplicate key in {constraint_name}")
        self._constraint_name = constraint_name

    @property
    def diag(self):
        return SimpleNamespace(constraint_name=self._constraint_name)


class HumanIdCursor:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = list(failures)
        self.attempted: list[str] = []
        self.executed: list[str] = []
        self.aborted = False
        self.row: dict[str, object] | None = None

    def __enter__(self) -> HumanIdCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: Sequence[object] = ()) -> None:
        normalized = _canonical(sql)
        self.attempted.append(normalized)
        if self.aborted and not normalized.startswith("rollback to savepoint"):
            raise AssertionError("statement attempted while transaction was aborted")
        if normalized.startswith("rollback to savepoint"):
            self.aborted = False
        self.executed.append(normalized)
        if normalized.startswith("insert into claims"):
            self.row = {"id": 41}
        elif normalized.startswith("update claims set human_id") and self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, UniqueViolation):
                self.aborted = True
            self.row = None
            raise failure
        else:
            self.row = None

    def fetchone(self) -> dict[str, object] | None:
        return self.row


class HumanIdConnection:
    def __init__(self, cursor: HumanIdCursor) -> None:
        self.cursor_instance = cursor

    def __enter__(self) -> HumanIdConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> HumanIdCursor:
        return self.cursor_instance


def _claim_store(
    monkeypatch: pytest.MonkeyPatch,
    failures: list[Exception],
) -> tuple[PostgresStore, HumanIdCursor, list[str]]:
    store = PostgresStore(
        "postgresql://runtime.invalid/memorymaster",
        tenant_id="tenant-a",
        require_tenant=True,
        principal="alice",
        allowed_scopes={"project:a"},
    )
    cursor = HumanIdCursor(failures)
    connection = HumanIdConnection(cursor)
    candidates = iter(("mm-collision", "mm-collision~2", "mm-collision~3"))
    events: list[str] = []
    monkeypatch.setattr(store, "connect", lambda: connection)
    monkeypatch.setattr(
        store,
        "_allocate_human_id",
        lambda *_args, **_kwargs: next(candidates),
    )
    monkeypatch.setattr(
        store,
        "_insert_event_row",
        lambda *_args, **_kwargs: events.append("event") or 1,
    )
    monkeypatch.setattr(
        store,
        "get_claim",
        lambda *_args, **_kwargs: SimpleNamespace(id=41, visibility="public"),
    )
    return store, cursor, events


def _create_claim(store: PostgresStore):
    return store.create_claim(
        "Human ID collision payload.",
        [CitationInput(source="identity-red", locator="fixture")],
        idempotency_key="human-id-transaction-key",
        subject="collision",
        scope="project:a",
        tenant_id="tenant-a",
        source_agent="alice",
        visibility="private",
    )


@pytest.mark.parametrize("index_name", sorted(HUMAN_ID_INDEXES))
def test_human_id_collision_uses_savepoint_then_retries_named_index_only(
    monkeypatch: pytest.MonkeyPatch,
    index_name: str,
) -> None:
    store, cursor, events = _claim_store(
        monkeypatch,
        [NamedUniqueViolation(index_name)],
    )

    claim = _create_claim(store)

    assert claim.id == 41
    assert any(sql.startswith("savepoint ") for sql in cursor.executed)
    assert any(sql.startswith("rollback to savepoint ") for sql in cursor.executed)
    assert any(sql.startswith("release savepoint ") for sql in cursor.executed)
    assert sum(sql.startswith("update claims set human_id") for sql in cursor.executed) == 2
    assert any(sql.startswith("insert into citations") for sql in cursor.executed)
    assert events == ["event"]


def test_foreign_unique_violation_is_not_swallowed_or_followed_by_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = NamedUniqueViolation("idx_claims_public_idempotency_key_unique")
    store, cursor, events = _claim_store(monkeypatch, [failure])

    with pytest.raises(UniqueViolation) as raised:
        _create_claim(store)

    assert raised.value is failure
    assert not any(sql.startswith("insert into citations") for sql in cursor.attempted)
    assert events == []


def test_non_unique_human_id_failure_is_never_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RuntimeError("human ID allocator unavailable")
    store, cursor, events = _claim_store(monkeypatch, [failure])

    with pytest.raises(RuntimeError, match="allocator unavailable"):
        _create_claim(store)

    assert not any(sql.startswith("insert into citations") for sql in cursor.attempted)
    assert events == []


def test_create_claim_default_visibility_is_consistent_across_backends() -> None:
    defaults = {
        inspect.signature(owner.create_claim).parameters["visibility"].default
        for owner in (_WriteClaimsMixin, PostgresStore)
    }
    defaults.add(inspect.signature(MemoryService.ingest).parameters["visibility"].default)

    assert defaults == {"public"}
