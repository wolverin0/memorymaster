"""Canonical connection helpers: open_conn / connect_ro pragma envelope.

WHY: ~55 raw ``sqlite3.connect`` sites across ~30 modules historically opened
the shared 3.47 GB DB with divergent settings (busy_timeout 0/5000/30000,
sometimes no WAL) — P1 spec F7. A writer with busy_timeout=0 that loses a
write race raises "database is locked" and LOSES the write; mixed journal
modes are a standing input to the 2026-06-05 btree corruption class. These
tests pin the single uniform envelope every connection must now share, and
that read-only consumers physically cannot take a write lock.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.stores._storage_shared import connect_ro, open_conn
from memorymaster.stores.storage import SQLiteStore


def _make_db(tmp_path: Path) -> str:
    """Create a minimal DB with one table + row for read/write probes."""
    db = str(tmp_path / "envelope.db")
    conn = open_conn(db)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO t (val) VALUES ('seed')")
        conn.commit()
    finally:
        conn.close()
    return db


def test_open_conn_sets_uniform_pragma_envelope(tmp_path: Path) -> None:
    """open_conn must set WAL + foreign_keys + busy_timeout>=15000 + Row factory.

    Intent: this is THE envelope every fleet writer shares. WAL lets readers
    proceed during writes; foreign_keys=ON prevents new orphan rows (401
    already on the live DB, spec F10); 15000ms busy_timeout makes write-race
    losers wait instead of dropping the write. Any regression here re-opens
    the divergent-pragma corruption input.
    """
    conn = open_conn(str(tmp_path / "envelope.db"))
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout_ms >= 15000, f"busy_timeout must be >= 15000ms, got {timeout_ms}"
    finally:
        conn.close()


def test_open_conn_honors_custom_busy_ms(tmp_path: Path) -> None:
    """Callers with special contention profiles can widen/narrow the timeout.

    Intent: the integrity checkpoint phase needs busy_timeout=30000 (spec
    §2.5.1) — the helper must support overriding without forking a new
    raw-connect site.
    """
    conn = open_conn(str(tmp_path / "envelope.db"), busy_ms=30000)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        conn.close()


def test_open_conn_retries_transient_open_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient open failure must be retried, not surfaced to the caller.

    Intent: the old SQLiteStore.connect wrapped its open in connect_with_retry
    (3 attempts, exponential backoff). Centralizing into open_conn must not
    silently drop that resilience — a momentary lock during a checkpoint
    would otherwise abort hooks/ingest fleet-wide.
    """
    monkeypatch.setenv("MEMORYMASTER_DB_RETRY_BASE", "0")
    real_connect = sqlite3.connect
    attempts = {"n": 0}

    def flaky(*args: object, **kwargs: object) -> sqlite3.Connection:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("memorymaster.stores._storage_shared.sqlite3.connect", flaky)
    conn = open_conn(str(tmp_path / "envelope.db"))
    try:
        assert attempts["n"] == 2, "first failure must be retried"
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_connect_ro_rejects_writes(tmp_path: Path) -> None:
    """A connect_ro connection must be physically unable to write.

    Intent: the recall hook runs on EVERY prompt; making it a guaranteed
    non-writer (mode=ro URI + query_only=ON) removes it from the writer fleet
    entirely (spec §2.2). If a write ever succeeds here, the RO guarantee is
    broken and the hook silently rejoins the corruption-prone writer set.
    """
    db = _make_db(tmp_path)
    conn = connect_ro(db)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t (val) VALUES ('forbidden')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE t SET val = 'forbidden'")
    finally:
        conn.close()


def test_connect_ro_reads_normally(tmp_path: Path) -> None:
    """connect_ro must still serve reads with Row access by column name.

    Intent: RO mode is a lock-discipline change, not a capability cut — the
    recall hook's query paths must see identical data shapes (sqlite3.Row)
    to the RW path or result-parity (spec step 8) breaks.
    """
    db = _make_db(tmp_path)
    conn = connect_ro(db)
    try:
        row = conn.execute("SELECT id, val FROM t").fetchone()
        assert row["val"] == "seed"
    finally:
        conn.close()


def test_store_connect_delegates_to_open_conn(tmp_path: Path) -> None:
    """SQLiteStore.connect() must carry the uniform envelope (15000ms, WAL, FK).

    Intent: the store is the main internal consumer; if its connect() drifts
    from open_conn (e.g. someone restores the old inline 5000ms body), the
    fleet splits into two pragma regimes again — exactly the F7 condition
    this step exists to kill.
    """
    store = SQLiteStore(tmp_path / "store.db")
    conn = store.connect()
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout_ms >= 15000, f"busy_timeout must be >= 15000ms, got {timeout_ms}"
    finally:
        conn.close()


def test_open_conn_check_same_thread_opt_out(tmp_path: Path) -> None:
    """open_conn(check_same_thread=False) must hand out a cross-thread conn.

    Intent: operator_queue shares ONE connection across threads behind its
    own locking; migrating it onto open_conn (step 3) must not regress that
    or every threaded consumer silently re-forks a raw-connect site.
    """
    import threading

    conn = open_conn(str(tmp_path / "threaded.db"), check_same_thread=False)
    errors: list[BaseException] = []

    def use_from_thread() -> None:
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
            conn.execute("INSERT INTO t (id) VALUES (1)")
            conn.commit()
        except BaseException as exc:  # noqa: BLE001 — recorded for the assert
            errors.append(exc)

    t = threading.Thread(target=use_from_thread)
    t.start()
    t.join()
    try:
        assert not errors, f"cross-thread use must work: {errors}"
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrated_defensive_readers_keep_empty_result_on_missing_db(tmp_path: Path) -> None:
    """Pure-reader sites migrated to connect_ro must keep their silent-fail contract.

    Intent: connect_ro raises OperationalError on a missing DB file (mode=ro
    cannot create one) whereas the old raw connect silently created an empty
    DB and the table query then failed soft. The recall path depends on the
    empty-result contract (claim_edges walk feeds the per-prompt hook, spec
    F9) — a raise here would abort recall fleet-wide. The migration wraps
    each defensive reader; this pins that the wrap exists.
    """
    from memorymaster.recall.claim_edges import walk_neighbors
    from memorymaster.knowledge.closets import search_closets

    missing = str(tmp_path / "does-not-exist.db")
    assert walk_neighbors(missing, [1], max_hops=1) == {}
    assert search_closets(missing, "anything") == []
    assert not Path(missing).exists(), "RO probe must not create the DB file"


# Files allowed to call sqlite3.connect directly (P1 spec step 3):
# - _storage_shared.py hosts the canonical helpers themselves.
# - snapshot.py uses the SQLite backup API, which needs raw same-process
#   connections without the WAL/busy envelope (a backup target must not be
#   flipped to WAL mid-copy, and backup sources are opened ephemerally).
_RAW_CONNECT_ALLOWED_FILES = {"_storage_shared.py", "snapshot.py"}


def test_no_bare_sqlite3_connect_outside_helpers() -> None:
    """No module in memorymaster/ may open SQLite connections ad hoc anymore.

    Intent: spec F7 — ~55 divergent raw-connect sites (busy_timeout
    0/5000/30000, sometimes no WAL) were a standing input to the 2026-06-05
    btree corruption. After step 3, every connection must come from
    open_conn/connect_ro so the whole fleet shares ONE pragma envelope.
    This sweep is the regression tripwire: any new `sqlite3.connect(` site
    outside the allowlist (helpers, snapshot backup API, explicit mode=ro
    URI readers) reintroduces the divergent-pragma class and must fail CI.
    """
    import memorymaster

    pkg_root = Path(memorymaster.__file__).resolve().parent
    offenders: list[str] = []
    for py in sorted(pkg_root.rglob("*.py")):
        if py.name in _RAW_CONNECT_ALLOWED_FILES:
            continue
        lines = py.read_text(encoding="utf-8", errors="replace").splitlines()
        for idx, line in enumerate(lines):
            if "sqlite3.connect(" not in line:
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # mode=ro URI sites are physically read-only — allowed (the call
            # may wrap across lines, so peek a short window forward).
            window = "\n".join(lines[idx:idx + 3])
            if "mode=ro" in window:
                continue
            offenders.append(f"{py.relative_to(pkg_root)}:{idx + 1}: {stripped}")
    assert not offenders, (
        "bare sqlite3.connect( found — route through "
        "memorymaster.stores._storage_shared.open_conn/connect_ro instead:\n"
        + "\n".join(offenders)
    )


def test_store_connect_ro_rejects_writes(tmp_path: Path) -> None:
    """SQLiteStore.connect_ro() must hand out a write-incapable connection.

    Intent: step 8 (RO recall) constructs the store in read-only mode and
    routes all reads through this method — it is the seam that takes the
    per-prompt recall hook out of the writer fleet, so it must enforce the
    same hard write rejection as the bare helper.
    """
    db = _make_db(tmp_path)
    store = SQLiteStore(db)
    conn = store.connect_ro()
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM t")
    finally:
        conn.close()


def test_init_db_closes_its_connections_deterministically(tmp_path):
    """init_db must not leak connections — WAL truncates without gc.collect().

    WHY: `with conn:` in sqlite3 is a TRANSACTION scope, not a closing one.
    A leaked init connection is only reclaimed by GC, so the WAL file stays
    at ~1.1MB until collection happens. That made wal_bytes-based integrity
    panels (and any WAL-size tripwire) nondeterministic: green in isolation,
    red when earlier tests shifted allocation patterns. The requirement is
    deterministic close — the WAL must be truncated/absent the moment
    init_db returns, with no garbage-collection assist.
    """
    import os

    from memorymaster.service import MemoryService

    db = tmp_path / "closes.db"
    svc = MemoryService(db)
    svc.init_db()
    wal = tmp_path / "closes.db-wal"
    wal_size = os.path.getsize(wal) if wal.exists() else 0
    assert wal_size == 0, (
        f"WAL is {wal_size} bytes after init_db — a connection leaked "
        "(close is GC-dependent, not deterministic)"
    )
