from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest


_CASE_ROOT = Path(".tmp_cases")
_POSTGRES_ADMIN_DSN_ENV = "MEMORYMASTER_TEST_POSTGRES_DSN"
_POSTGRES_APP_DSN_ENV = "MEMORYMASTER_TEST_POSTGRES_APP_DSN"
_POSTGRES_DISPOSABLE_ENV = "MEMORYMASTER_TEST_POSTGRES_RLS_DISPOSABLE"
_LIVE_POSTGRES_DSN_ENVS = ("DATABASE_URL", "POSTGRES_DSN", "MEMORYMASTER_POSTGRES_DSN")


@dataclass(frozen=True)
class _DisposablePostgresRuntime:
    admin_dsn: str
    app_dsn: str


def _same_secret(left: str, right: str) -> bool:
    return bool(left and right) and secrets.compare_digest(left, right)


def _require_disposable_postgres_runtime() -> _DisposablePostgresRuntime:
    admin_dsn = os.getenv(_POSTGRES_ADMIN_DSN_ENV, "").strip()
    app_dsn = os.getenv(_POSTGRES_APP_DSN_ENV, "").strip()
    opted_in = os.getenv(_POSTGRES_DISPOSABLE_ENV, "").strip() == "1"
    if not admin_dsn or not app_dsn or not opted_in:
        pytest.skip(
            "BLOCKED-EXTERNAL: Postgres parity requires distinct admin/app DSNs "
            f"and {_POSTGRES_DISPOSABLE_ENV}=1"
        )
    if _same_secret(admin_dsn, app_dsn):
        pytest.fail("Postgres parity requires distinct admin and app DSNs.")
    for env_name in _LIVE_POSTGRES_DSN_ENVS:
        live_dsn = os.getenv(env_name, "").strip()
        if _same_secret(admin_dsn, live_dsn) or _same_secret(app_dsn, live_dsn):
            pytest.fail(f"Refusing to reuse {env_name} for disposable Postgres tests.")
    return _DisposablePostgresRuntime(admin_dsn=admin_dsn, app_dsn=app_dsn)


def _database_identity(psycopg: Any, dsn: str) -> tuple[object, ...]:
    with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_database(), current_user, rolsuper, rolbypassrls,
                   rolreplication, rolcreaterole, rolcreatedb
            FROM pg_roles WHERE rolname = current_user
            """
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("Postgres connection identity could not be verified.")
    return tuple(row)


def _validate_disposable_postgres_roles(
    admin_identity: tuple[object, ...],
    app_identity: tuple[object, ...],
) -> None:
    if str(admin_identity[0]) != str(app_identity[0]):
        pytest.fail("Admin and app DSNs must target the same disposable database.")
    if str(admin_identity[1]) == str(app_identity[1]):
        pytest.fail("Admin and app DSNs must authenticate as distinct roles.")
    if not bool(admin_identity[2]) and not bool(admin_identity[3]):
        pytest.fail("The Postgres migrator must be SUPERUSER or BYPASSRLS.")
    if any(bool(value) for value in app_identity[2:]):
        pytest.fail("The Postgres app role has a forbidden role attribute.")


def _grant_disposable_event_contract(psycopg: Any, admin_dsn: str, app_role: str) -> None:
    from psycopg import sql

    role = sql.Identifier(app_role)
    with psycopg.connect(admin_dsn) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE public.events TO {}").format(role))
        cur.execute(
            sql.SQL(
                "REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
                "ON TABLE public.events FROM {}"
            ).format(role)
        )
        cur.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION public.memorymaster_event_chain_head() TO {}"
            ).format(role)
        )


@lru_cache(maxsize=4)
def _initialize_disposable_postgres(admin_dsn: str, app_dsn: str) -> None:
    try:
        import psycopg
    except ImportError:
        pytest.skip("BLOCKED-EXTERNAL: psycopg is required for Postgres parity tests")

    try:
        admin_identity = _database_identity(psycopg, admin_dsn)
        app_identity = _database_identity(psycopg, app_dsn)
    except psycopg.OperationalError:
        pytest.skip("BLOCKED-EXTERNAL: configured Postgres test DSNs are unreachable")
    _validate_disposable_postgres_roles(admin_identity, app_identity)

    from memorymaster.stores.postgres_store import PostgresStore

    PostgresStore(admin_dsn).init_db()
    _grant_disposable_event_contract(psycopg, admin_dsn, str(app_identity[1]))


@pytest.fixture(autouse=True)
def _explicit_local_mcp_auth(monkeypatch) -> None:
    """Make legacy MCP test calls exercise the named local-trusted profile."""
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")


# ---------------------------------------------------------------------------
# Backend parametrization for parity tests (v3.20.0-S2)
# ---------------------------------------------------------------------------
#
# `parametrize_backends` yields a fresh MemoryService on each backend so the
# SAME test body runs against both SQLite and Postgres and must produce the
# same observable result. SQLite always runs (file-based, no server). Postgres
# runs only with distinct admin/app DSNs plus explicit disposable opt-in;
# otherwise that parametrization is skipped so offline dev machines stay green.

def _fresh_sqlite_service(tmp_path):
    from memorymaster.core.service import MemoryService

    db = tmp_path / "parity-sqlite.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    return svc


def _fresh_postgres_service():
    from memorymaster.core.service import MemoryService

    config = _require_disposable_postgres_runtime()
    _initialize_disposable_postgres(config.admin_dsn, config.app_dsn)
    run_id = uuid4().hex
    return MemoryService(
        config.app_dsn,
        workspace_root=".",
        tenant_id=f"parity-{run_id}",
        require_tenant=True,
        principal="parity-test",
        allowed_scopes=("project",),
    )


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.postgres),
    ]
)
def parametrize_backends(request, tmp_path):
    """Yield (backend_name, MemoryService) for each available backend.

    Use in parity tests so one test body asserts identical observable
    behaviour on both SQLite and Postgres.
    """
    backend = request.param
    if backend == "sqlite":
        yield backend, _fresh_sqlite_service(tmp_path)
    else:
        yield backend, _fresh_postgres_service()


def _prune_case_root(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.unlink()
        except OSError:
            continue
    for directory in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            continue


@pytest.fixture(autouse=True)
def _hermetic_snapshot_dir(tmp_path_factory, monkeypatch) -> None:
    """Keep the integrity phase's VACUUM INTO snapshots out of the real home.

    WHY: run_cycle now ends with the integrity steward phase (P1 spec §2.5);
    without this redirect every test that calls run_cycle would write a
    mm-YYYYMMDD.db snapshot of its tiny tmp DB under the user's real
    ~/.memorymaster/snapshots/ — and the keep-3 rotation could evict REAL
    production snapshots. Tests must never touch operator recovery artifacts.
    """
    monkeypatch.setenv(
        "MEMORYMASTER_SNAPSHOT_DIR",
        str(tmp_path_factory.mktemp("mm-snapshots")),
    )


@pytest.fixture(autouse=True)
def _hermetic_wal_discipline(tmp_path_factory, monkeypatch) -> None:
    """Neutralize the WAL-discipline dogfood flag and redirect the spool root.

    WHY: the P1 rollout (spec §5) sets MEMORYMASTER_WAL_DISCIPLINE=1
    machine-wide (setx) for the dogfood week. Without this reset, every
    legacy direct-path test (dream bridge, verbatim store, recall) would
    silently flip into the spool regime on the operator's machine and fail;
    and any test that spools without setting MEMORYMASTER_SPOOL_DIR would
    litter envelopes under the REAL ~/.memorymaster/spool — where the
    production steward's drain would replay tmp-DB test claims. Tests opt
    back into the flag per-test via monkeypatch.setenv.
    """
    monkeypatch.delenv("MEMORYMASTER_WAL_DISCIPLINE", raising=False)
    # The init_db fast-path sub-flag is ALSO setx'd machine-wide for the
    # dogfood: under it a re-init skips the _ensure_* passes, so any test
    # that re-runs init_db to trigger a backfill (e.g. human_id) silently
    # no-ops and fails. Same hermeticity rule: tests opt in explicitly.
    monkeypatch.delenv("MEMORYMASTER_INITDB_FASTPATH", raising=False)
    monkeypatch.setenv(
        "MEMORYMASTER_SPOOL_DIR",
        str(tmp_path_factory.mktemp("mm-spool")),
    )


@pytest.fixture(autouse=True)
def _cleanup_case_artifacts() -> None:
    _CASE_ROOT.mkdir(parents=True, exist_ok=True)
    # Don't prune before the test - files might be locked from previous tests
    # which causes database corruption when mkstemp reuses the path
    yield
    # Only prune after the test to clean up this test's artifacts
    _prune_case_root(_CASE_ROOT)
