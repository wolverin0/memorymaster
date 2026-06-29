"""archive_by_source — lifecycle-safe bulk source cleanup (plan 3.2a).

WHY this matters: eval/backfill runs pollute the store with throwaway claims.
The obvious fix — a bulk hard-delete — would violate MemoryMaster's core
invariant: claims terminate at `archived`, never `DELETE FROM claims` (the
bitemporal/audit trail depends on it). So cleanup ARCHIVES by source instead of
deleting. These tests anchor on the invariants that make that safe: (1) it
archives, never deletes (rows still exist, just `archived`); (2) it touches ONLY
the named source; (3) dry-run changes nothing; (4) a re-run is a clean no-op;
(5) a cap never silently truncates.

Decided this session over a hard-delete delete_by_source (re-survey 2026-06-24).
"""
from __future__ import annotations

import pytest

from memorymaster.core.service import MemoryService
from memorymaster.core.models import CitationInput


@pytest.fixture
def svc(tmp_path):
    s = MemoryService(db_target=str(tmp_path / "archive.db"), workspace_root=tmp_path)
    s.init_db()
    return s


def _ingest(svc, text, source_agent):
    return svc.ingest(
        text, [CitationInput(source="test")], scope="project:test", source_agent=source_agent,
    )


def test_dry_run_reports_but_changes_nothing(svc):
    a = _ingest(svc, "eval claim one", "eval-batch")
    b = _ingest(svc, "eval claim two", "eval-batch")
    res = svc.store.archive_by_source("eval-batch", dry_run=True)
    assert res["dry_run"] is True
    assert res["matched"] == 2
    assert res["archived"] == 0
    # Nothing actually changed — both still live.
    assert svc.store.get_claim(a.id).status != "archived"
    assert svc.store.get_claim(b.id).status != "archived"


def test_real_run_archives_not_deletes(svc):
    a = _ingest(svc, "eval claim one", "eval-batch")
    res = svc.store.archive_by_source("eval-batch", dry_run=False)
    assert res["archived"] == 1
    # The row STILL EXISTS (not deleted) — it is merely archived. This is the
    # whole point of choosing archive over hard-delete.
    reloaded = svc.store.get_claim(a.id)
    assert reloaded is not None
    assert reloaded.status == "archived"


def test_only_named_source_is_touched(svc):
    keep = _ingest(svc, "a real durable fact", "claude-session")
    doomed = _ingest(svc, "throwaway backfill row", "backfill-2026")
    svc.store.archive_by_source("backfill-2026", dry_run=False)
    assert svc.store.get_claim(doomed.id).status == "archived"
    assert svc.store.get_claim(keep.id).status != "archived"  # untouched


def test_rerun_is_a_clean_noop(svc):
    _ingest(svc, "eval claim one", "eval-batch")
    first = svc.store.archive_by_source("eval-batch", dry_run=False)
    assert first["archived"] == 1
    second = svc.store.archive_by_source("eval-batch", dry_run=False)
    # Already-archived rows are excluded — re-running matches nothing.
    assert second["matched"] == 0
    assert second["archived"] == 0


def test_limit_never_silently_truncates(svc):
    for i in range(3):
        _ingest(svc, f"eval claim {i}", "eval-batch")
    res = svc.store.archive_by_source("eval-batch", dry_run=True, limit=2)
    assert res["matched"] == 2
    assert res["truncated"] is True  # caller is TOLD the set was capped
