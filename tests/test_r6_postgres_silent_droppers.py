"""R6 — four sites of one silent-dropper, pinned.

``PostgresStore`` subclasses ``SQLiteStore`` and inherits ``?``-placeholder SQL
that psycopg rejects outright. Every caller of the affected writes suppressed
the exception, so on a Postgres deployment ``access_count``, ``claims.entity_id``
and the whole entity registry read ZERO forever, with no error, no counter and
no log. The project already fixed this once for ``recompute_tiers``; these tests
pin the pattern so the next instance cannot ship quietly.

The dialect tests run EVERYWHERE, not only where a Postgres DSN is configured:
``_PgLikeConnection`` is a psycopg-shaped wrapper over SQLite that rejects ``?``
the way Postgres does and hands back mapping rows the way ``dict_row`` does.
Real-Postgres assertions live in ``tests/test_backend_parity.py`` and skip
without a DSN.
"""
from __future__ import annotations

import sqlite3

import pytest

from memorymaster.core import access_recording, observability
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.knowledge import entity_ingest
from memorymaster.knowledge.entity_registry import add_alias, resolve_or_create
from memorymaster.stores._storage_lifecycle import _LifecycleMixin
from memorymaster.stores.postgres_store import PostgresStore


# ---------------------------------------------------------------------------
# psycopg-shaped test double
# ---------------------------------------------------------------------------


class _PgLikeCursor:
    """Rows as mappings, like a psycopg cursor opened with ``dict_row``."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def __getattr__(self, name: str):
        if name == "lastrowid":
            # psycopg cursors have no lastrowid; anything relying on it is
            # sqlite-only code that would break on the real backend.
            raise AttributeError("psycopg cursors expose no lastrowid")
        return getattr(self._cursor, name)

    def fetchone(self):
        row = self._cursor.fetchone()
        return None if row is None else dict(row)

    def fetchall(self):
        return [dict(row) for row in self._cursor.fetchall()]


class _PgLikeConnection:
    """SQLite underneath, psycopg semantics on the surface.

    Rejects ``?`` placeholders (Postgres parses a bare ``?`` as an operator and
    raises a syntax error) and accepts ``%s``. Deliberately NOT a
    ``sqlite3.Connection`` subclass, so ``entity_registry._is_sqlite`` sees it
    as the foreign backend it is standing in for.
    """

    def __init__(self, sqlite_conn: sqlite3.Connection) -> None:
        self._conn = sqlite_conn

    def execute(self, sql: str, params=()):
        if "?" in sql:
            raise RuntimeError(
                f'syntax error at or near "?" — psycopg rejects sqlite '
                f"placeholders: {' '.join(sql.split())[:90]}"
            )
        return _PgLikeCursor(self._conn.execute(sql.replace("%s", "?"), params))

    def commit(self) -> None:
        self._conn.commit()

    def __enter__(self) -> "_PgLikeConnection":
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture()
def pg_like_conn(tmp_path):
    """A registry-schema database exposed through the psycopg-shaped wrapper."""
    from memorymaster.stores.storage import SQLiteStore

    db = tmp_path / "pg-like.db"
    SQLiteStore(str(db)).init_db()
    raw = sqlite3.connect(str(db))
    raw.row_factory = sqlite3.Row
    try:
        yield _PgLikeConnection(raw)
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# Site 1 + 2: record_access / record_accesses_batch, and every sibling
# ---------------------------------------------------------------------------


def _lifecycle_methods_with_sqlite_placeholders() -> list[str]:
    """Names of ``_LifecycleMixin`` methods whose SQL uses ``?`` placeholders."""
    import ast
    import sys
    from pathlib import Path

    source = Path(sys.modules[_LifecycleMixin.__module__].__file__).read_text(
        encoding="utf-8"
    )
    lines = source.splitlines()
    class_node = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == _LifecycleMixin.__name__
    )
    names: list[str] = []
    for node in class_node.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "?" in "\n".join(lines[node.lineno - 1 : node.end_lineno]):
            names.append(node.name)
    return names


def test_postgres_overrides_every_sqlite_dialect_lifecycle_write() -> None:
    """No inherited ``?``-placeholder write may reach a Postgres connection.

    Derived, not a hand-kept list: whenever a ``_LifecycleMixin`` method uses
    sqlite placeholders, ``PostgresStore`` must define its own. Before this
    fix, ``record_access``, ``record_accesses_batch`` and (the newly extracted)
    ``set_claim_entity_id`` were gaps — each one a write that raised on every
    call and was swallowed by its caller.
    """
    candidates = _lifecycle_methods_with_sqlite_placeholders()
    assert candidates, "scan found no ?-placeholder methods — the scan is broken"
    gaps = [name for name in candidates if name not in PostgresStore.__dict__]
    assert gaps == [], (
        "PostgresStore inherits sqlite-dialect SQL for: "
        + ", ".join(gaps)
        + " — psycopg will reject these at runtime"
    )


def test_dropped_access_write_is_counted_not_suppressed(tmp_path) -> None:
    """A failed access write must leave a trace.

    The recall path wrapped this in ``contextlib.suppress(Exception)`` with no
    else-branch and no counter, so a permanently-failing Postgres write looked
    exactly like a healthy one. ``recompute_tiers`` then reads the frozen
    ``access_count = 0`` and demotes the aging corpus to ``peripheral``.
    """
    svc = MemoryService(tmp_path / "mm.db", workspace_root=tmp_path)
    svc.init_db()
    claim = svc.ingest(
        text="accessed claim",
        citations=[CitationInput(source="s://a", locator="l", excerpt="e")],
        source_agent="r6-test",
    )

    def _boom(_claim_ids):
        raise RuntimeError("syntax error at or near \"?\"")

    svc.store.record_accesses_batch = _boom  # type: ignore[method-assign]
    observability.reset_metrics()
    svc._record_accesses([{"claim": claim}], query_text="accessed")

    assert observability.metric_value(
        access_recording.ACCESS_WRITE_FAILED, backend="SQLiteStore"
    ) == 1


def test_successful_access_write_leaves_the_counter_alone(tmp_path) -> None:
    """The counter must mean "a write was dropped", not "a write happened"."""
    svc = MemoryService(tmp_path / "mm.db", workspace_root=tmp_path)
    svc.init_db()
    claim = svc.ingest(
        text="healthy access path",
        citations=[CitationInput(source="s://a", locator="l", excerpt="e")],
        source_agent="r6-test",
    )
    observability.reset_metrics()
    svc._record_accesses([{"claim": claim}], query_text="healthy")

    assert observability.metric_family_total(access_recording.ACCESS_WRITE_FAILED) == 0
    assert svc.store.get_claim(claim.id).access_count == 1


# ---------------------------------------------------------------------------
# Site 3: claims.entity_id, written raw from the service layer
# ---------------------------------------------------------------------------


def test_ingest_links_the_claim_to_its_entity(tmp_path) -> None:
    """End-to-end on the working backend: the link is actually written."""
    svc = MemoryService(tmp_path / "mm.db", workspace_root=tmp_path)
    svc.init_db()
    claim = svc.ingest(
        text="Qdrant is the vector index",
        citations=[CitationInput(source="s://a", locator="l", excerpt="e")],
        source_agent="r6-test",
        subject="Qdrant",
    )
    with svc.store.connect() as conn:
        entity_id = conn.execute(
            "SELECT entity_id FROM claims WHERE id = ?", (claim.id,)
        ).fetchone()[0]
    assert entity_id, "claim was not linked to its canonical entity"


def test_dropped_entity_link_is_counted_not_suppressed(tmp_path) -> None:
    svc = MemoryService(tmp_path / "mm.db", workspace_root=tmp_path)
    svc.init_db()

    class _RefusingStore:
        def set_claim_entity_id(self, claim_id: int, entity_id: int) -> bool:
            raise RuntimeError("syntax error at or near \"?\"")

    observability.reset_metrics()
    linked = entity_ingest.link_claim_entity(_RefusingStore(), 1, 42)

    assert linked is False
    assert observability.metric_value(
        entity_ingest.ENTITY_LINK_FAILED, backend="_RefusingStore"
    ) == 1


# ---------------------------------------------------------------------------
# Site 4: entity_registry called with a non-sqlite connection
# ---------------------------------------------------------------------------


def test_resolve_or_create_runs_on_a_psycopg_shaped_connection(pg_like_conn) -> None:
    """The registry must not assume sqlite placeholders or positional rows.

    Without the dialect seam this raises on its FIRST statement — the alias
    SELECT — which is exactly what happened on every Postgres ingest.
    """
    entity_id = resolve_or_create(
        pg_like_conn, "MemoryMaster", entity_type="project", scope="project:mm"
    )
    assert entity_id > 0

    # Idempotent: a second call with a different surface form collapses onto
    # the same entity and records the variant as an alias.
    again = resolve_or_create(pg_like_conn, "memory master", scope="project:mm")
    assert again > 0
    same = resolve_or_create(pg_like_conn, "MemoryMaster", scope="project:mm")
    assert same == entity_id

    rows = pg_like_conn.execute(
        "SELECT COUNT(*) AS n FROM entity_aliases WHERE entity_id = %s", (entity_id,)
    ).fetchone()
    assert rows["n"] >= 1


def test_add_alias_runs_on_a_psycopg_shaped_connection(pg_like_conn) -> None:
    entity_id = resolve_or_create(pg_like_conn, "Qdrant", scope="project:mm")
    assert entity_id > 0
    assert add_alias(pg_like_conn, entity_id, "qdrant-cloud") is True
    # Deduped by the (entity_id, variant_key) UNIQUE constraint on both
    # backends — ON CONFLICT DO NOTHING must behave like INSERT OR IGNORE.
    assert add_alias(pg_like_conn, entity_id, "qdrant-cloud") is False


def test_fuzzy_resolver_reads_mapping_rows(pg_like_conn, monkeypatch) -> None:
    """The opt-in fuzzy path unpacked rows positionally — dict rows broke it."""
    monkeypatch.setenv("MEMORYMASTER_ENTITY_FUZZY_RESOLVE", "1")
    original = resolve_or_create(pg_like_conn, "MemoryMaster", scope="project:mm")
    assert original > 0
    collapsed = resolve_or_create(pg_like_conn, "MemoryMastr", scope="project:mm")
    assert collapsed == original, "near-miss should collapse onto the same entity"


def test_failed_entity_resolution_is_counted_not_suppressed() -> None:
    class _RefusingStore:
        def connect(self):
            raise RuntimeError("syntax error at or near \"?\"")

    observability.reset_metrics()
    entity_id = entity_ingest.resolve_claim_entities(
        _RefusingStore(),
        subject="Qdrant",
        text="Qdrant is the vector index",
        claim_type="fact",
        scope="project:mm",
    )

    assert entity_id == 0
    assert observability.metric_value(
        entity_ingest.ENTITY_RESOLUTION_FAILED, backend="_RefusingStore"
    ) == 1
