"""Tests for incremental delta sync (export_delta + CLI export-delta).

Coverage:
- export_delta on a fresh DB writes a valid merge source
- watermark filtering: only claims with updated_at > since are exported
- empty delta when nothing changed
- citations travel with their claims, claim_id linkage preserved
- the delta file round-trips through merge_databases (delta IS a merge source)
- CLI `export-delta` writes the file and reports counts + next watermark
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from memorymaster.bridges.delta_sync import export_delta
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


@pytest.fixture
def populated_db(tmp_path) -> Iterator[tuple[Path, MemoryService]]:
    """A memorymaster DB with a few claims at known timestamps."""
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


def _count(db: Path, table: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# export_delta core
# ---------------------------------------------------------------------------


def test_export_delta_full_when_since_empty(populated_db, tmp_path):
    db, svc = populated_db
    _ingest(svc, "claim alpha")
    _ingest(svc, "claim beta")

    out = tmp_path / "delta.db"
    result = export_delta(db, "", out)

    assert result["exported"] == 2
    assert out.exists()
    assert _count(out, "claims") == 2


def _set_updated_at(db: Path, text: str, iso: str) -> None:
    """Deterministically pin a claim's updated_at — avoids clock-granularity
    races in watermark tests."""
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE claims SET updated_at = ? WHERE text = ?", (iso, text))
    conn.commit()
    conn.close()


def test_export_delta_watermark_filters_old_claims(populated_db, tmp_path):
    db, svc = populated_db
    _ingest(svc, "old claim")
    _ingest(svc, "new claim after watermark")

    # Pin timestamps deterministically: old well before the watermark,
    # new well after. No reliance on wall-clock granularity.
    _set_updated_at(db, "old claim", "2020-01-01T00:00:00+00:00")
    _set_updated_at(db, "new claim after watermark", "2026-05-18T12:00:00+00:00")

    out = tmp_path / "delta.db"
    result = export_delta(db, "2026-01-01T00:00:00+00:00", out)

    # Only the new claim is after the watermark
    assert result["exported"] == 1
    rows = sqlite3.connect(str(out)).execute("SELECT text FROM claims").fetchall()
    assert rows == [("new claim after watermark",)]


def test_export_delta_watermark_is_inclusive(populated_db, tmp_path):
    """A claim whose updated_at EXACTLY equals the watermark must be
    re-exported (>= semantics) — never skipped. Skipping = data loss."""
    db, svc = populated_db
    _ingest(svc, "boundary claim")
    _set_updated_at(db, "boundary claim", "2026-05-18T12:00:00+00:00")

    out = tmp_path / "delta.db"
    result = export_delta(db, "2026-05-18T12:00:00+00:00", out)

    assert result["exported"] == 1  # boundary claim is included, not skipped


def test_export_delta_empty_when_nothing_changed(populated_db, tmp_path):
    db, svc = populated_db
    _ingest(svc, "only claim")

    # Watermark strictly in the future — nothing is newer, delta is empty.
    out = tmp_path / "delta.db"
    result = export_delta(db, "2099-01-01T00:00:00+00:00", out)

    assert result["exported"] == 0
    assert result["citations"] == 0
    assert result["max_updated_at"] is None
    # File still created, just empty of claims
    assert _count(out, "claims") == 0


def test_export_delta_carries_citations(populated_db, tmp_path):
    db, svc = populated_db
    _ingest(svc, "claim with citation")

    out = tmp_path / "delta.db"
    result = export_delta(db, "", out)

    assert result["exported"] == 1
    assert result["citations"] >= 1
    # claim_id linkage preserved: the citation's claim_id matches a claim id
    conn = sqlite3.connect(str(out))
    claim_ids = {r[0] for r in conn.execute("SELECT id FROM claims").fetchall()}
    cit_claim_ids = {r[0] for r in conn.execute("SELECT claim_id FROM citations").fetchall()}
    conn.close()
    assert cit_claim_ids.issubset(claim_ids)


def test_export_delta_returns_max_updated_at_as_next_watermark(populated_db, tmp_path):
    db, svc = populated_db
    _ingest(svc, "claim one")
    _ingest(svc, "claim two")

    out = tmp_path / "delta.db"
    result = export_delta(db, "", out)

    # max_updated_at must equal the actual max in the source
    conn = sqlite3.connect(str(db))
    actual_max = conn.execute("SELECT MAX(updated_at) FROM claims").fetchone()[0]
    conn.close()
    assert result["max_updated_at"] == actual_max


def test_export_delta_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        export_delta(tmp_path / "does-not-exist.db", "", tmp_path / "out.db")


def test_export_delta_overwrites_stale_output(populated_db, tmp_path):
    db, svc = populated_db
    _ingest(svc, "claim")
    out = tmp_path / "delta.db"
    out.write_text("stale junk")  # pre-existing non-DB file
    result = export_delta(db, "", out)
    assert result["exported"] == 1  # overwritten cleanly, no corruption


# ---------------------------------------------------------------------------
# The delta file is a valid merge source — round-trip through merge_databases
# ---------------------------------------------------------------------------


def test_delta_file_round_trips_through_merge(populated_db, tmp_path):
    """The whole point: export a delta on side A, merge it into side B,
    and side B ends up with side A's new claims — without copying the
    full DB."""
    db_a, svc_a = populated_db
    _ingest(svc_a, "shared baseline claim")

    # Side B starts as a separate fresh DB
    db_b = tmp_path / "side_b.db"
    svc_b = MemoryService(db_b, workspace_root=tmp_path)
    svc_b.init_db()
    _ingest(svc_b, "shared baseline claim")  # same baseline so it's not "new"

    # Side A gets a brand-new claim. Pin timestamps deterministically so the
    # watermark cleanly separates the baseline from the new claim.
    _set_updated_at(db_a, "shared baseline claim", "2026-05-18T10:00:00+00:00")
    _ingest(svc_a, "side A exclusive new claim")
    _set_updated_at(db_a, "side A exclusive new claim", "2026-05-18T14:00:00+00:00")

    # Export only the delta from A (watermark between baseline and new claim)
    delta = tmp_path / "a_delta.db"
    result = export_delta(db_a, "2026-05-18T12:00:00+00:00", delta)
    assert result["exported"] == 1

    # Merge the small delta into side B
    from memorymaster.bridges.db_merge import merge_databases
    merge_result = merge_databases(str(db_b), str(delta))
    assert merge_result["merged"] == 1

    # Side B now has the exclusive claim
    texts = {
        r[0]
        for r in sqlite3.connect(str(db_b)).execute("SELECT text FROM claims").fetchall()
    }
    assert "side A exclusive new claim" in texts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_export_delta_writes_file_and_reports(populated_db, tmp_path, capsys):
    from memorymaster.surfaces.cli import main

    db, svc = populated_db
    _ingest(svc, "cli delta claim")

    out = tmp_path / "cli_delta.db"
    rc = main(["--db", str(db), "--workspace", str(tmp_path), "export-delta", "--output", str(out)])
    assert rc == 0
    assert out.exists()
    captured = capsys.readouterr()
    assert "export-delta: 1 claims" in captured.out
    assert "next watermark" in captured.out


def test_cli_export_delta_json_output(populated_db, tmp_path, capsys):
    import json
    from memorymaster.surfaces.cli import main

    db, svc = populated_db
    _ingest(svc, "json delta claim")

    out = tmp_path / "json_delta.db"
    rc = main(["--json", "--db", str(db), "--workspace", str(tmp_path), "export-delta", "--output", str(out)])
    assert rc == 0
    captured = capsys.readouterr()
    # Envelope wraps the result; just assert the export count is reachable
    assert '"exported": 1' in captured.out or '"exported":1' in captured.out


def test_cli_export_delta_empty_reports_no_change(populated_db, tmp_path, capsys):
    from memorymaster.surfaces.cli import main

    db, svc = populated_db
    _ingest(svc, "single claim")

    # Future watermark — nothing newer, CLI should report the empty delta.
    out = tmp_path / "empty_delta.db"
    rc = main([
        "--db", str(db), "--workspace", str(tmp_path),
        "export-delta", "--since", "2099-01-01T00:00:00+00:00", "--output", str(out),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "delta is empty" in captured.out
