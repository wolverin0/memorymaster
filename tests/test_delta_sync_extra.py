"""Extra edge-case coverage for ``memorymaster.bridges.delta_sync.export_delta``.

These tests deliberately complement ``tests/test_delta_sync.py`` by drilling
into the four contract corners that are easiest to break silently:

1. **Watermark boundary (``>=`` not ``>``)** — a claim whose ``updated_at``
   EXACTLY equals the watermark MUST be re-exported, never skipped. Skipping a
   boundary claim is silent data loss when several claims share a same-second
   timestamp. We also prove a claim one tick BELOW the watermark is excluded,
   so the boundary is the real ``>=`` cut, not "export everything".
2. **DDL / CREATE-TABLE synthesis** — the delta file must carry safe,
   value-only tables with ordered source-column parity.
3. **Empty export** — nothing newer than the watermark yields zero rows,
   ``max_updated_at is None``, yet a valid (schema-only) file still exists.
4. **Full export** — empty/whitespace ``since`` exports every claim and
   reports the true max ``updated_at`` as the next watermark.

All tests build their own tmp SQLite DB from the real project schema via
``MemoryService.init_db`` — no shared global DB, no network, no LLM, no
Postgres. ``updated_at`` is pinned by direct UPDATE so assertions never race
the wall clock.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from memorymaster.bridges.delta_sync import export_delta
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors tests/test_delta_sync.py style)
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_db(tmp_path) -> Iterator[tuple[Path, MemoryService]]:
    """A fresh memorymaster DB built from the real schema."""
    db = tmp_path / "full.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    yield db, svc


def _ingest(svc: MemoryService, text: str, **kw) -> object:
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="test://src", locator="loc", excerpt="ex")],
        source_agent="test",
        **kw,
    )


def _set_updated_at(db: Path, text: str, iso: str) -> None:
    """Pin a claim's ``updated_at`` deterministically (no clock races)."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE claims SET updated_at = ? WHERE text = ?", (iso, text))
        conn.commit()
    finally:
        conn.close()


def _texts(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT text FROM claims").fetchall()}
    finally:
        conn.close()


def _table_ddl(db: Path, table: str) -> str | None:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _table_columns(db: Path, table: str) -> list[tuple[str, str]]:
    with sqlite3.connect(str(db)) as conn:
        return [
            (str(row[1]), str(row[2]).upper())
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        ]


# ---------------------------------------------------------------------------
# 1. Watermark boundary — the >= vs > edge
# ---------------------------------------------------------------------------


def test_boundary_claim_at_exact_watermark_is_exported(populated_db, tmp_path):
    """CONTRACT: ``updated_at >=`` watermark. A claim whose timestamp EXACTLY
    equals the watermark is included — never skipped. Skipping it would be
    silent data loss. This anchors on the inclusive boundary: if the source
    ever switched to a strict ``>``, this claim would vanish and the test
    must fail."""
    db, svc = populated_db
    _ingest(svc, "exactly at watermark")
    _set_updated_at(db, "exactly at watermark", "2026-05-18T12:00:00+00:00")

    out = tmp_path / "delta.db"
    result = export_delta(db, "2026-05-18T12:00:00+00:00", out)

    assert result["exported"] == 1
    assert _texts(out) == {"exactly at watermark"}


def test_claim_one_tick_below_watermark_is_excluded(populated_db, tmp_path):
    """CONTRACT: the watermark is a real ``>=`` cut, not "export everything".
    A claim strictly OLDER than the watermark must NOT appear in the delta.
    Paired with the boundary test above, this proves the comparison operator
    is ``>=`` and not, say, ``>=`` degraded to an always-true filter."""
    db, svc = populated_db
    _ingest(svc, "just below watermark")
    # One second before the watermark.
    _set_updated_at(db, "just below watermark", "2026-05-18T11:59:59+00:00")

    out = tmp_path / "delta.db"
    result = export_delta(db, "2026-05-18T12:00:00+00:00", out)

    assert result["exported"] == 0
    assert _texts(out) == set()


def test_multiple_claims_sharing_boundary_timestamp_all_exported(
    populated_db, tmp_path
):
    """CONTRACT: same-second ingest can give several claims an identical
    ``updated_at``. ALL of them at the boundary must export — this is exactly
    the data-loss scenario ``>=`` exists to prevent. A strict ``>`` would drop
    every one of them."""
    db, svc = populated_db
    _ingest(svc, "twin A")
    _ingest(svc, "twin B")
    _ingest(svc, "twin C")
    for t in ("twin A", "twin B", "twin C"):
        _set_updated_at(db, t, "2026-05-18T12:00:00+00:00")

    out = tmp_path / "delta.db"
    result = export_delta(db, "2026-05-18T12:00:00+00:00", out)

    assert result["exported"] == 3
    assert _texts(out) == {"twin A", "twin B", "twin C"}


def test_watermark_advances_to_max_updated_at(populated_db, tmp_path):
    """CONTRACT: ``max_updated_at`` is the newest ``updated_at`` actually
    exported and is meant to be fed back as the NEXT watermark. It must be the
    real maximum among exported rows so the next cycle starts exactly at the
    frontier. Anchors on the watermark-advance requirement, not on row order."""
    db, svc = populated_db
    _ingest(svc, "middle")
    _ingest(svc, "newest")
    _ingest(svc, "oldest")
    _set_updated_at(db, "oldest", "2026-05-18T08:00:00+00:00")
    _set_updated_at(db, "middle", "2026-05-18T12:00:00+00:00")
    _set_updated_at(db, "newest", "2026-05-18T20:00:00+00:00")

    out = tmp_path / "delta.db"
    result = export_delta(db, "2026-05-18T10:00:00+00:00", out)

    # oldest is below the watermark; middle + newest exported.
    assert result["exported"] == 2
    assert result["max_updated_at"] == "2026-05-18T20:00:00+00:00"
    # The returned watermark, re-used as the next `since`, must re-include
    # only the boundary claim (>= semantics), proving it is a usable frontier.
    out2 = tmp_path / "delta2.db"
    next_result = export_delta(db, result["max_updated_at"], out2)
    assert _texts(out2) == {"newest"}
    assert next_result["exported"] == 1


# ---------------------------------------------------------------------------
# 2. DDL / CREATE-TABLE copy into the delta
# ---------------------------------------------------------------------------


def test_delta_carries_create_table_ddl_for_both_tables(populated_db, tmp_path):
    """CONTRACT: the delta is a standalone merge source, so it MUST contain
    verbatim ``CREATE TABLE`` statements for both ``claims`` and ``citations``.
    Without the DDL the merge engine cannot read the file. Anchors on the
    'valid merge source' requirement."""
    db, svc = populated_db
    _ingest(svc, "with ddl")

    out = tmp_path / "delta.db"
    export_delta(db, "", out)

    claims_ddl = _table_ddl(out, "claims")
    citations_ddl = _table_ddl(out, "citations")
    assert claims_ddl is not None and claims_ddl.startswith("CREATE TABLE")
    assert citations_ddl is not None and citations_ddl.startswith("CREATE TABLE")


def test_delta_ddl_is_synthesized_from_source_columns(populated_db, tmp_path):
    """Transport DDL preserves ordered columns but no source constraints."""
    db, svc = populated_db
    _ingest(svc, "schema parity")

    out = tmp_path / "delta.db"
    export_delta(db, "", out)

    for table in ("claims", "citations"):
        assert [name for name, _ in _table_columns(out, table)] == [
            name for name, _ in _table_columns(db, table)
        ]
        ddl = _table_ddl(out, table) or ""
        assert "FOREIGN KEY" not in ddl.upper()
        assert "CHECK" not in ddl.upper()


def test_ddl_copied_even_when_export_is_empty(populated_db, tmp_path):
    """CONTRACT: DDL is copied BEFORE the watermark filter runs, so even an
    empty delta (zero claims) is still a schema-valid SQLite file. A consumer
    must be able to open it and find the tables. This guards the ordering of
    'copy schema' vs 'select rows'."""
    db, svc = populated_db
    _ingest(svc, "lonely")

    out = tmp_path / "delta.db"
    result = export_delta(db, "2099-01-01T00:00:00+00:00", out)

    assert result["exported"] == 0
    # Schema present despite zero rows.
    assert _table_ddl(out, "claims") is not None
    assert _table_ddl(out, "citations") is not None


def test_ddl_copy_fails_on_db_without_claims_table(tmp_path):
    """CONTRACT: ``_copy_table_ddl`` raises ValueError when a required table is
    absent — protecting against pointing export at a non-memorymaster DB.
    Anchors on the explicit 'not a memorymaster DB?' guard, not a generic
    sqlite error."""
    bogus = tmp_path / "bogus.db"
    conn = sqlite3.connect(str(bogus))
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError):
        export_delta(bogus, "", tmp_path / "out.db")


# ---------------------------------------------------------------------------
# 3. Empty export — nothing newer than the watermark
# ---------------------------------------------------------------------------


def test_empty_export_reports_zero_and_null_watermark(populated_db, tmp_path):
    """CONTRACT: when no claim is newer-or-equal to the watermark the result is
    exported=0, citations=0, max_updated_at=None. ``None`` signals 'no new
    frontier' to the caller so it keeps its previous watermark — a wrong
    sentinel (e.g. "") would corrupt the next cycle's filter."""
    db, svc = populated_db
    _ingest(svc, "present claim")
    _set_updated_at(db, "present claim", "2026-01-01T00:00:00+00:00")

    out = tmp_path / "delta.db"
    result = export_delta(db, "2030-01-01T00:00:00+00:00", out)

    assert result["exported"] == 0
    assert result["citations"] == 0
    assert result["max_updated_at"] is None
    assert result["since"] == "2030-01-01T00:00:00+00:00"


def test_empty_export_writes_no_claim_or_citation_rows(populated_db, tmp_path):
    """CONTRACT: an empty delta contains zero claim AND zero citation rows —
    citations only travel WITH their exported claims, so no claims means no
    orphan citations leak into the delta."""
    db, svc = populated_db
    _ingest(svc, "claim with cite")
    _set_updated_at(db, "claim with cite", "2026-01-01T00:00:00+00:00")

    out = tmp_path / "delta.db"
    export_delta(db, "2030-01-01T00:00:00+00:00", out)

    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0] == 0
    finally:
        conn.close()


def test_empty_export_on_completely_empty_source(populated_db, tmp_path):
    """CONTRACT: a brand-new DB with no claims at all (even full export) yields
    an empty delta with a None frontier — the function must not blow up on the
    zero-row path before it builds INSERT statements."""
    db, _svc = populated_db  # init_db ran, but nothing ingested

    out = tmp_path / "delta.db"
    result = export_delta(db, "", out)  # full export of an empty DB

    assert result["exported"] == 0
    assert result["max_updated_at"] is None
    # File still valid: tables exist.
    assert _table_ddl(out, "claims") is not None


# ---------------------------------------------------------------------------
# 4. Full export — no watermark / export everything
# ---------------------------------------------------------------------------


def test_full_export_when_since_empty_exports_all_claims(populated_db, tmp_path):
    """CONTRACT: empty ``since`` => full bootstrap export. EVERY claim is
    exported regardless of timestamp, because the watermark branch is skipped
    entirely. Anchors on 'no filter' behaviour, not on a generous threshold."""
    db, svc = populated_db
    _ingest(svc, "a")
    _ingest(svc, "b")
    _ingest(svc, "c")
    # Spread timestamps wide to prove none are filtered.
    _set_updated_at(db, "a", "2001-01-01T00:00:00+00:00")
    _set_updated_at(db, "b", "2026-05-18T12:00:00+00:00")
    _set_updated_at(db, "c", "2099-12-31T23:59:59+00:00")

    out = tmp_path / "delta.db"
    result = export_delta(db, "", out)

    assert result["exported"] == 3
    assert _texts(out) == {"a", "b", "c"}


def test_full_export_whitespace_since_is_treated_as_full(populated_db, tmp_path):
    """CONTRACT: ``since`` is stripped before use, so a whitespace-only value
    ("  ") is equivalent to empty => full export. This anchors on the
    ``.strip()`` normalisation; a literal whitespace watermark would otherwise
    sort BELOW every ISO timestamp and only accidentally behave the same."""
    db, svc = populated_db
    _ingest(svc, "ws one")
    _ingest(svc, "ws two")

    out = tmp_path / "delta.db"
    result = export_delta(db, "   ", out)

    assert result["exported"] == 2
    # `since` is echoed back stripped, confirming normalisation happened.
    assert result["since"] == ""


def test_full_export_reports_true_max_updated_at(populated_db, tmp_path):
    """CONTRACT: on a full export ``max_updated_at`` equals the real MAX over
    all claims in the source — that becomes the first real watermark for the
    next incremental cycle. Compare against an independent MAX() query so the
    test fails if the loop computes the frontier wrong."""
    db, svc = populated_db
    _ingest(svc, "x")
    _ingest(svc, "y")
    _set_updated_at(db, "x", "2026-05-18T03:00:00+00:00")
    _set_updated_at(db, "y", "2026-05-18T21:00:00+00:00")

    out = tmp_path / "delta.db"
    result = export_delta(db, "", out)

    conn = sqlite3.connect(str(db))
    try:
        actual_max = conn.execute("SELECT MAX(updated_at) FROM claims").fetchone()[0]
    finally:
        conn.close()
    assert result["max_updated_at"] == actual_max == "2026-05-18T21:00:00+00:00"


def test_full_export_carries_citations_with_preserved_claim_ids(
    populated_db, tmp_path
):
    """CONTRACT: a full export brings every claim's citations along with the
    ORIGINAL claim ids intact, so claim_id->claim linkage survives into the
    delta (required for the merge engine). No citation may reference a claim id
    absent from the delta."""
    db, svc = populated_db
    _ingest(svc, "cited one")
    _ingest(svc, "cited two")

    out = tmp_path / "delta.db"
    result = export_delta(db, "", out)

    assert result["citations"] >= 2
    conn = sqlite3.connect(str(out))
    try:
        claim_ids = {r[0] for r in conn.execute("SELECT id FROM claims").fetchall()}
        cit_claim_ids = {
            r[0] for r in conn.execute("SELECT claim_id FROM citations").fetchall()
        }
    finally:
        conn.close()
    assert cit_claim_ids and cit_claim_ids.issubset(claim_ids)
