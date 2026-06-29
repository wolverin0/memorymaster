"""Bitemporal write-time guard (plan 2.1).

WHY this matters: a claim row with ``valid_until`` before ``valid_from`` is
*durable but invisible* — it persists in the DB yet is silently excluded from
every time-filtered read, so the operator believes it was stored when it can
never be recalled. Malformed ISO-8601 in a temporal field has the same effect
once downstream code tries to parse it. The guard rejects both at the ingest
boundary, loudly, before the row reaches the store. These tests anchor on that
data-integrity requirement, not on the parser's internals.

Borrowed from MemPalace's write-time interval validation (re-survey 2026-06-24).
"""
from __future__ import annotations

import pytest

from memorymaster.core.models import validate_temporal_fields
from memorymaster.core.service import MemoryService
from memorymaster.core.models import CitationInput


# --- pure validator -------------------------------------------------------

def test_inverted_interval_is_rejected():
    """valid_until before valid_from must raise — this is the invisible-row bug."""
    with pytest.raises(ValueError, match="valid_until"):
        validate_temporal_fields(None, "2026-06-24T00:00:00Z", "2026-06-01T00:00:00Z")


def test_well_ordered_interval_passes():
    """A normal [from, until] interval is accepted unchanged."""
    validate_temporal_fields(None, "2026-06-01T00:00:00Z", "2026-06-24T00:00:00Z")


def test_equal_bounds_pass():
    """valid_from == valid_until is a zero-width-but-valid interval, not inverted."""
    validate_temporal_fields(None, "2026-06-24T00:00:00Z", "2026-06-24T00:00:00Z")


def test_missing_fields_pass():
    """All-None (the common case — fields auto-populate later) must not raise."""
    validate_temporal_fields(None, None, None)
    validate_temporal_fields(None, "2026-06-01T00:00:00Z", None)  # open-ended


@pytest.mark.parametrize("field_vals", [
    ("not-a-date", None, None),          # event_time malformed
    (None, "2026-13-99", None),          # valid_from malformed
    (None, None, "yesterday"),           # valid_until malformed
])
def test_malformed_iso_is_rejected(field_vals):
    """Non-ISO-8601 temporal input must raise, not silently corrupt the row."""
    with pytest.raises(ValueError):
        validate_temporal_fields(*field_vals)


def test_mixed_tz_awareness_does_not_crash_comparison():
    """A naive valid_from vs tz-aware valid_until must compare cleanly (treated
    as UTC), not raise TypeError — otherwise the guard itself would be the bug."""
    validate_temporal_fields(None, "2026-06-01T00:00:00", "2026-06-24T00:00:00Z")
    with pytest.raises(ValueError, match="valid_until"):
        validate_temporal_fields(None, "2026-06-24T00:00:00Z", "2026-06-01T00:00:00")


# --- ingest integration (real service, tmp DB) ----------------------------

@pytest.fixture
def svc(tmp_path):
    s = MemoryService(db_target=str(tmp_path / "bitemporal.db"), workspace_root=tmp_path)
    s.init_db()
    return s


def test_ingest_rejects_inverted_interval(svc):
    """The guard fires through the real ingest path — the inverted row never
    reaches the store (ValueError surfaces as a VALIDATION_ERROR to callers)."""
    with pytest.raises(ValueError, match="valid_until"):
        svc.ingest(
            "Subscription is valid until last month",
            [CitationInput(source="test")],
            scope="project:test",
            valid_from="2026-06-24T00:00:00Z",
            valid_until="2026-05-01T00:00:00Z",
        )


def test_ingest_accepts_valid_interval(svc):
    """A well-ordered interval ingests normally and is retrievable."""
    claim = svc.ingest(
        "Contract runs June through December",
        [CitationInput(source="test")],
        scope="project:test",
        valid_from="2026-06-01T00:00:00Z",
        valid_until="2026-12-31T00:00:00Z",
    )
    assert claim.id > 0
    assert svc.store.get_claim(claim.id) is not None
