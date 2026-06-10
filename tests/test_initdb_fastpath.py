"""init_db fast-path tests (P1 WAL-discipline spec step 10, §2.9).

WHY: ``init_db`` runs ``executescript`` + 14 ``_ensure_*`` passes + a
MigrationRunner probe on EVERY cold start — measured 16.06 s on the live
3.47 GB DB (F2). The fast-path stamps ``PRAGMA user_version`` with a
fingerprint of the full schema lineage (schema.sql bytes + every
migration's version/checksum) after a successful full init, and skips the
redundant passes when the stamp matches. Skipping ``_ensure_*`` on a
LAGGING DB is the one genuinely risky optimization in the program, so
these tests pin the safety requirements, not the implementation:

- a stamp mismatch (older fingerprint, hand-set garbage, fresh DB at 0)
  MUST force the full path — fast-path may only fire on a provably
  up-to-date DB;
- a schema.sql change MUST change the fingerprint with no manual bump
  step — a "stamp bump someone forgot" can never exist;
- a new migration file MUST change the fingerprint the same way;
- flag OFF (default) MUST be byte-identical legacy behavior: full passes
  every time, user_version never written.
"""
from __future__ import annotations

import sqlite3
import types
from pathlib import Path

import pytest

import memorymaster.migrations
import memorymaster.migrations.runner
import memorymaster.storage as storage_mod
from memorymaster.storage import SQLiteStore, schema_stamp

FLAG = "MEMORYMASTER_INITDB_FASTPATH"


@pytest.fixture()
def counters(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count full-path work: one representative ``_ensure_*`` pass + the
    MigrationRunner probe. Fast-path = neither increments."""
    counts = {"ensure": 0, "migrate": 0}

    # _ensure_fts5_schema is a @staticmethod on _SchemaMixin: class access
    # yields the bare function(conn), and the replacement must be re-wrapped
    # as staticmethod or instance calls would inject self as conn.
    real_ensure = SQLiteStore._ensure_fts5_schema

    def counting_ensure(conn):  # noqa: ANN001 - mirrors mixin signature
        counts["ensure"] += 1
        return real_ensure(conn)

    monkeypatch.setattr(SQLiteStore, "_ensure_fts5_schema", staticmethod(counting_ensure))

    real_apply = memorymaster.migrations.MigrationRunner.apply_pending

    def counting_apply(self):  # noqa: ANN001
        counts["migrate"] += 1
        return real_apply(self)

    monkeypatch.setattr(
        memorymaster.migrations.MigrationRunner, "apply_pending", counting_apply
    )
    return counts


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _set_user_version(db: Path, value: int) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(f"PRAGMA user_version = {int(value)}")
        conn.commit()
    finally:
        conn.close()


def test_flag_off_keeps_legacy_full_path_and_never_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counters: dict[str, int]
) -> None:
    """Default OFF = the untouched v3.27 behavior: every init runs the full
    passes and user_version stays at the SQLite default 0. The flag must be
    invisible until an operator opts in."""
    monkeypatch.delenv(FLAG, raising=False)
    db = tmp_path / "legacy.db"
    store = SQLiteStore(db)
    store.init_db()
    store.init_db()
    assert counters["ensure"] == 2
    assert counters["migrate"] == 2
    assert _user_version(db) == 0


def test_fastpath_init_on_stamped_db_skips_ensure_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counters: dict[str, int]
) -> None:
    """The point of §2.9: cold init on an already-stamped DB must skip the
    14 ``_ensure_*`` passes AND the MigrationRunner probe (16.06 s → <2 s
    target). First init is full (fresh DB: user_version 0 ≠ stamp) and
    stamps; second init does zero schema work."""
    monkeypatch.setenv(FLAG, "1")
    db = tmp_path / "fast.db"
    store = SQLiteStore(db)
    store.init_db()
    assert counters["ensure"] == 1
    assert counters["migrate"] == 1
    assert _user_version(db) == schema_stamp() != 0

    store.init_db()
    assert counters["ensure"] == 1, "stamped DB must not re-run _ensure_* passes"
    assert counters["migrate"] == 1, "stamped DB must not re-probe migrations"

    # The skipped init must still leave a usable store (claims table intact).
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_stamp_mismatch_forces_full_path_and_restamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counters: dict[str, int]
) -> None:
    """A DB stamped under ANY other fingerprint (older release, hand-set
    garbage) is by definition not provably up to date — the fast-path must
    fall through to the full ``_ensure_*`` + migration path and re-stamp,
    never trust a foreign stamp."""
    monkeypatch.setenv(FLAG, "1")
    db = tmp_path / "mismatch.db"
    store = SQLiteStore(db)
    store.init_db()
    assert counters["ensure"] == 1

    _set_user_version(db, 12345)  # simulate a stale/foreign stamp
    store.init_db()
    assert counters["ensure"] == 2, "mismatched stamp must force the full path"
    assert counters["migrate"] == 2
    assert _user_version(db) == schema_stamp()


def test_schema_change_without_manual_bump_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counters: dict[str, int]
) -> None:
    """The fingerprint is DERIVED from schema.sql bytes, so an edited schema
    invalidates old stamps automatically — there is no manual stamp constant
    a contributor could forget, which is what makes skipping ``_ensure_*``
    safe at all."""
    monkeypatch.setenv(FLAG, "1")
    db = tmp_path / "schemachange.db"
    store = SQLiteStore(db)
    store.init_db()
    assert counters["ensure"] == 1
    old_stamp = _user_version(db)

    real_load = storage_mod.load_schema_sql
    monkeypatch.setattr(
        storage_mod,
        "load_schema_sql",
        lambda: real_load() + "\n-- v-next: simulated schema edit\n",
    )
    assert schema_stamp() != old_stamp, "schema edit must change the fingerprint"

    store.init_db()
    assert counters["ensure"] == 2, "schema change must force the full path"
    assert _user_version(db) == schema_stamp() != old_stamp


def test_new_migration_changes_stamp_and_forces_full_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counters: dict[str, int]
) -> None:
    """Adding a migration file must bump the fingerprint with no extra step
    (spec step 10: 'stamp bump on new migration'): a DB stamped before the
    migration existed must take the full path so the migration is applied."""
    monkeypatch.setenv(FLAG, "1")
    db = tmp_path / "newmig.db"
    store = SQLiteStore(db)
    store.init_db()
    old_stamp = _user_version(db)

    mig_src = tmp_path / "9999_synthetic.py"
    mig_src.write_text("# synthetic migration body\n", encoding="utf-8")
    applied = {"n": 0}

    def _apply(conn) -> None:  # noqa: ANN001
        applied["n"] += 1

    fake = memorymaster.migrations.runner.Migration(
        version=9999,
        description="synthetic fast-path test migration",
        module_name="tests.synthetic_9999",
        source_path=mig_src,
        apply_sqlite=_apply,
        apply_postgres=_apply,
    )
    real_discover = memorymaster.migrations.discover_migrations

    def discover_plus_fake(
        package: str = "memorymaster.migrations",
    ) -> list[memorymaster.migrations.runner.Migration]:
        return [*real_discover(package), fake]

    # Patch every namespace that resolves discover_migrations at call time:
    # schema_stamp imports from the package; MigrationRunner.apply_pending
    # resolves it from the runner module's globals.
    for ns in (memorymaster.migrations, memorymaster.migrations.runner):
        assert isinstance(ns, types.ModuleType)
        monkeypatch.setattr(ns, "discover_migrations", discover_plus_fake)

    assert schema_stamp() != old_stamp, "new migration must change the fingerprint"

    store.init_db()
    assert applied["n"] == 1, "full path must actually apply the new migration"
    assert _user_version(db) == schema_stamp() != old_stamp
