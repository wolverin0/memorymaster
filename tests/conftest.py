from __future__ import annotations

import os
from pathlib import Path

import pytest


_CASE_ROOT = Path(".tmp_cases")


# ---------------------------------------------------------------------------
# Backend parametrization for parity tests (v3.20.0-S2)
# ---------------------------------------------------------------------------
#
# `parametrize_backends` yields a fresh MemoryService on each backend so the
# SAME test body runs against both SQLite and Postgres and must produce the
# same observable result. SQLite always runs (file-based, no server). Postgres
# runs only when MEMORYMASTER_TEST_POSTGRES_DSN is set; otherwise that
# parametrization is skipped so dev machines without a Postgres stay green.

def _pg_dsn() -> str | None:
    return os.getenv("MEMORYMASTER_TEST_POSTGRES_DSN")


def _fresh_sqlite_service(tmp_path):
    from memorymaster.core.service import MemoryService

    db = tmp_path / "parity-sqlite.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    return svc


def _fresh_postgres_service():
    from memorymaster.core.service import MemoryService

    dsn = _pg_dsn()
    if not dsn:
        pytest.skip("MEMORYMASTER_TEST_POSTGRES_DSN is not set")
    svc = MemoryService(dsn, workspace_root=".")
    svc.init_db()
    # Deterministic start: clear claims/citations/events (+ optional tables).
    with svc.store.connect() as conn:
        with conn.cursor() as cur:
            for tbl in ("claim_links", "claim_embeddings"):
                cur.execute("SELECT to_regclass(%s) AS t", (f"public.{tbl}",))
                row = cur.fetchone()
                present = (row["t"] if isinstance(row, dict) else row[0]) is not None
                if present:
                    cur.execute(f"DELETE FROM {tbl}")
            cur.execute("DELETE FROM citations")
            cur.execute("DELETE FROM events")
            cur.execute("DELETE FROM claims")
    return svc


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
