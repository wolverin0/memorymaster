"""R9 — an event whose content is the ABSENCE of an effect must not be written.

WHY THIS EXISTS. Measured on the production DB (2,416,145 events, 2026-08-18):

    confidence / "deterministic_adjust=+0.000"                489,927   20.3%
    deterministic_validator / payload {}                      461,257   19.1%

Two rows per claim per pass, both saying "I looked and changed nothing",
together **39% of the entire event log** at ~15k/day. This is not merely waste:
it is the flood that collapsed the operational health check's scan window to
13.9 minutes (R1) and that threatens every other bounded scan (R4).

The counters survive; only the rows are dropped.

Fail-without-fix status, verified by stashing the fix: 4 of the 6 tests below
fail on the unfixed tree. The other two -- `test_a_real_adjustment_is_still_
recorded` and `test_the_first_deterministic_check_is_still_recorded` -- pass
either way BY DESIGN. They do not pin R9; they bound the fix, so a later
"optimisation" cannot turn row suppression into audit-trail deletion or break
the dashboard's validation-latency metric.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core import observability
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import deterministic


def _svc(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "r9.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _claim(svc: MemoryService, text: str = "a claim with no deterministic predicate"):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="test", locator="loc")],
        source_agent="test-agent",
    )


def _confidence_events(svc: MemoryService, claim_id: int) -> list:
    return [
        e
        for e in svc.list_events(claim_id=claim_id, limit=500)
        if e.event_type == "confidence"
    ]


def _deterministic_events(svc: MemoryService, claim_id: int) -> list:
    return [
        e
        for e in svc.list_events(claim_id=claim_id, limit=500)
        if e.event_type == "deterministic_validator"
    ]


@pytest.mark.unit
def test_unchanged_confidence_writes_no_event(tmp_path: Path) -> None:
    """The exact production row: `set_confidence` to the value already stored.

    Before the fix this wrote a `confidence` row reading
    `deterministic_adjust=+0.000` -- 489,927 of them in production.
    """
    svc = _svc(tmp_path)
    claim = _claim(svc)
    before = len(_confidence_events(svc, claim.id))

    svc.store.set_confidence(claim.id, claim.confidence, details="deterministic_adjust=+0.000")

    assert len(_confidence_events(svc, claim.id)) == before, (
        "an adjustment that changed nothing was recorded as an event"
    )


@pytest.mark.unit
def test_a_real_adjustment_is_still_recorded(tmp_path: Path) -> None:
    """The suppression must be exactly 'nothing changed', not 'small change'.

    Without this, a fix for R9 could silently delete the audit trail it was only
    supposed to de-duplicate.
    """
    svc = _svc(tmp_path)
    claim = _claim(svc)
    before = len(_confidence_events(svc, claim.id))

    moved = min(1.0, claim.confidence + 0.05)
    svc.store.set_confidence(claim.id, moved, details=f"deterministic_adjust={moved - claim.confidence:+.3f}")

    events = _confidence_events(svc, claim.id)
    assert len(events) == before + 1, "a real confidence change was dropped"
    assert svc.store.get_claim(claim.id).confidence == pytest.approx(moved)


@pytest.mark.unit
def test_the_suppressed_write_is_still_counted(tmp_path: Path) -> None:
    """'Drop the row, keep the metric.' A silent drop would be a NEW inert
    signal: volume that stops being reported anywhere at all."""
    svc = _svc(tmp_path)
    claim = _claim(svc)
    observability.reset_metrics()

    svc.store.set_confidence(claim.id, claim.confidence, details="deterministic_adjust=+0.000")

    assert observability.metric_family_total("claim_confidence_noop_total") == 1
    assert "claim_confidence_noop_total" in observability.metrics_text()


@pytest.mark.unit
def test_repeated_deterministic_passes_stop_writing_rows(tmp_path: Path) -> None:
    """The whole R9 shape, end to end, through the job that produces it.

    A claim with no checkable predicate is re-examined every cycle. Before the
    fix each pass wrote two rows (a +0.000 confidence row and an empty-payload
    deterministic_validator row) forever. After it, later passes write nothing.
    """
    svc = _svc(tmp_path)
    claim = _claim(svc)

    deterministic.run(svc.store, workspace_root=tmp_path, limit=50)
    after_first = len(svc.list_events(claim_id=claim.id, limit=500))

    for _ in range(5):
        deterministic.run(svc.store, workspace_root=tmp_path, limit=50)
    after_repeats = len(svc.list_events(claim_id=claim.id, limit=500))

    assert after_repeats == after_first, (
        f"5 further no-op passes wrote {after_repeats - after_first} events recording "
        "that nothing changed"
    )


@pytest.mark.unit
def test_the_first_deterministic_check_is_still_recorded(tmp_path: Path) -> None:
    """Not a nicety: dashboard._validation_latency_metric reads
    MIN(created_at) over validator events per claim. Keeping the FIRST row per
    claim leaves that metric bit-for-bit unchanged while dropping the repeats.
    """
    svc = _svc(tmp_path)
    claim = _claim(svc)

    deterministic.run(svc.store, workspace_root=tmp_path, limit=50)

    assert len(_deterministic_events(svc, claim.id)) == 1, (
        "the claim's first deterministic check must remain in the log"
    )


@pytest.mark.unit
def test_the_job_reports_what_it_suppressed(tmp_path: Path) -> None:
    """`checked` still counts the work; `unchanged`/`no_signal` account for the
    rows that were not written, so the drop is reported rather than invisible."""
    svc = _svc(tmp_path)
    _claim(svc)
    deterministic.run(svc.store, workspace_root=tmp_path, limit=50)

    result = deterministic.run(svc.store, workspace_root=tmp_path, limit=50)

    assert result["checked"] >= 1, "the pass still ran"
    assert result["unchanged"] >= 1
    assert result["no_signal"] >= 1
