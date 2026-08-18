"""Stale FTS entries must be detected, not left to rot silently.

``claims_fts`` is an external-content FTS5 index. Its update trigger clears a
row's OLD terms before writing the new ones, which only works while the index
still holds those OLD terms — so the first stale entry makes every later edit
preserve the staleness instead of clearing it. Drift accumulates and never
self-corrects.

Nothing else catches it: the claims themselves are intact, ``quick_check``
passes, and the only symptom is recall returning confident-looking matches that
have nothing to do with the query. On a production database in 2026-08 roughly
45% of matches for common terms were stale, which is what buried real answers
under irrelevant ones.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import integrity


def _svc(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "drift.db", workspace_root=tmp_path)
    svc.init_db()
    svc.ingest(
        text="The quokka bridge resolves telemetry handles.",
        citations=[CitationInput(source="test", locator="l", excerpt="e")],
        scope="project:test",
        source_agent="pytest",
    )
    return svc


def _drift_the_index(db_path: Path) -> None:
    """Edit a claim with the sync triggers dropped — how real drift happens."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS trg_claims_fts_update;
        UPDATE claims SET text = 'entirely different subject matter' WHERE id = 1;
        """
    )
    conn.commit()
    conn.close()


def test_healthy_index_reports_no_drift(tmp_path):
    svc = _svc(tmp_path)
    result = integrity.fts_drift(svc.store, svc.store.db_path, force=True)
    assert result["drifted"] is False


def test_stale_index_is_detected(tmp_path):
    svc = _svc(tmp_path)
    _drift_the_index(Path(svc.store.db_path))

    # The symptom, before the detector: the index still matches the old word.
    stale = svc.store.list_claims(text_query="quokka", limit=5, status_in=["candidate"])
    assert stale and "quokka" not in stale[0].text.lower(), (
        "precondition: a stale entry should still match a word the claim no "
        "longer contains"
    )

    result = integrity.fts_drift(svc.store, svc.store.db_path, force=True)
    assert result["drifted"] is True
    assert "rebuild" in str(result["repair"])


def test_drift_does_not_freeze_promotions(tmp_path):
    """A stale index is a search problem, not corruption. Governance continues."""
    svc = _svc(tmp_path)
    _drift_the_index(Path(svc.store.db_path))
    integrity.fts_drift(svc.store, svc.store.db_path, force=True)
    assert integrity.promotions_frozen(svc.store.db_path) is False


def test_the_documented_repair_actually_fixes_it(tmp_path):
    """The remedy the detector reports must be the one that works."""
    svc = _svc(tmp_path)
    _drift_the_index(Path(svc.store.db_path))
    assert integrity.fts_drift(svc.store, svc.store.db_path, force=True)["drifted"] is True

    conn = sqlite3.connect(str(svc.store.db_path))
    conn.execute(integrity.FTS_REBUILD_SQL)
    conn.commit()
    conn.close()

    assert integrity.fts_drift(svc.store, svc.store.db_path, force=True)["drifted"] is False
    assert svc.store.list_claims(text_query="quokka", limit=5, status_in=["candidate"]) == []


def test_check_is_throttled_between_runs(tmp_path):
    """It must not re-scan the index on every steward cycle."""
    svc = _svc(tmp_path)
    assert integrity.fts_drift(svc.store, svc.store.db_path, force=True)["drifted"] is False
    assert integrity.fts_drift(svc.store, svc.store.db_path) == {"skipped": "throttled"}


@pytest.mark.unit
def test_missing_fts_table_is_skipped_not_failed(tmp_path):
    svc = _svc(tmp_path)
    conn = sqlite3.connect(str(svc.store.db_path))
    conn.executescript("DROP TABLE claims_fts;")
    conn.commit()
    conn.close()
    assert integrity.fts_drift(svc.store, svc.store.db_path, force=True) == {
        "skipped": "no_fts_table"
    }
