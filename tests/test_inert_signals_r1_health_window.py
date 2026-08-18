"""R1 — the health check must examine a time window, not a row count.

WHY THIS EXISTS: `_provider_failure_count` scanned `list_events(limit=1000)`
with no filter. The events table is dominated by bookkeeping — 487,927
`deterministic_adjust=+0.000` rows out of 2,410,028 in production — so those
1000 rows spanned **13.9 minutes** (measured 2026-08-18, 01:54:41 → 02:08:38).
A provider outage twenty minutes old was structurally invisible, and the check
reported healthy.

That is the failure this module exists to detect, occurring inside the detector.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.operational_health import _provider_failure_count


def _svc(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "health.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _claim(svc: MemoryService):
    return svc.ingest(
        text="anchor claim for event attachment",
        citations=[CitationInput(source="test", locator="loc")],
        source_agent="test-agent",
    )


@pytest.mark.unit
def test_provider_failure_is_found_under_a_flood_of_bookkeeping(tmp_path: Path) -> None:
    """The production shape: one real failure, buried under routine noise.

    With a row cap the failure falls outside the window and the check reports 0
    — a silent all-clear. With a time bound it is found.
    """
    svc = _svc(tmp_path)
    claim = _claim(svc)

    svc.store.record_event(
        claim_id=claim.id,
        event_type="policy_decision",
        details="llm provider unavailable: gemini returned 503",
        payload={"provider": "gemini", "status": 503},
    )
    # Bury it, the way the real log does.
    for _ in range(1200):
        svc.store.record_event(
            claim_id=claim.id,
            event_type="confidence",
            details="deterministic_adjust=+0.000",
        )

    result = _provider_failure_count(svc)

    assert result["count"] == 1, "the provider failure was buried by bookkeeping noise"
    assert result["window_hours"] == 24


@pytest.mark.unit
def test_scan_reports_what_it_actually_examined(tmp_path: Path) -> None:
    """A bare 0 is unreadable: it means both "nothing failed" and "the check
    saw almost nothing". The result must expose the window it covered."""
    svc = _svc(tmp_path)
    claim = _claim(svc)
    for _ in range(5):
        svc.store.record_event(
            claim_id=claim.id, event_type="confidence", details="deterministic_adjust=+0.000"
        )

    result = _provider_failure_count(svc)

    assert result["count"] == 0
    assert result["events_scanned"] >= 5, "must report how much it looked at"
    assert "oldest_event_examined" in result
    assert result["window_hours"] > 0


@pytest.mark.unit
def test_the_scan_is_bounded_by_time_not_by_row_count(tmp_path: Path) -> None:
    """The precise defect: the old code passed only `limit` and no time bound,
    so the window was whatever 1000 rows happened to span. Pin that a `since`
    is passed and that it matches the declared window -- the events table is
    append-only (and hash-chained), so backdating a row is not available to us.
    """
    svc = _svc(tmp_path)
    captured: dict[str, object] = {}
    real = svc.list_events

    def _spy(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    svc.list_events = _spy  # type: ignore[method-assign]
    result = _provider_failure_count(svc, window_hours=6)

    assert captured.get("since") is not None, "scan ran with no time bound"
    since = datetime.fromisoformat(str(captured["since"]))
    expected = datetime.now(timezone.utc) - timedelta(hours=6)
    assert abs((since - expected).total_seconds()) < 120, "window does not match what was declared"
    assert result["window_hours"] == 6
    # And the row cap must be large enough not to become the real bound again.
    assert int(captured.get("limit", 0)) >= 10_000


@pytest.mark.unit
def test_since_filter_reaches_both_backends(tmp_path: Path) -> None:
    """`since` is the shared primitive R4 also depends on; it must exist on the
    store API, not just be simulated in Python by the caller."""
    svc = _svc(tmp_path)
    claim = _claim(svc)
    svc.store.record_event(claim_id=claim.id, event_type="audit", details="recent marker")

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    assert svc.list_events(limit=50, since=future) == []
    assert any(e.details == "recent marker" for e in svc.list_events(limit=50, since=past))
