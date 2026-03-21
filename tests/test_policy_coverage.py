"""Tests for memorymaster.policy — coverage gaps."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from memorymaster.policy import (
    _age_seconds,
    _parse_iso,
    _priority_score,
    select_revalidation_candidates,
)


class TestParseIso:
    def test_none(self):
        assert _parse_iso(None) is None

    def test_empty(self):
        assert _parse_iso("") is None

    def test_invalid(self):
        assert _parse_iso("not-a-date") is None

    def test_naive_datetime(self):
        result = _parse_iso("2026-01-15T10:00:00")
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_aware_datetime(self):
        result = _parse_iso("2026-01-15T10:00:00+00:00")
        assert result is not None


class TestAgeSeconds:
    def test_no_dates_returns_zero(self):
        claim = MagicMock()
        claim.last_validated_at = None
        claim.updated_at = None
        claim.created_at = None
        assert _age_seconds(claim, datetime.now(timezone.utc)) == 0.0


class TestSelectRevalidation:
    def test_unknown_mode_raises(self):
        store = MagicMock()
        with pytest.raises(ValueError, match="Unknown policy mode"):
            select_revalidation_candidates(store, mode="unknown")

    def test_zero_limit(self):
        store = MagicMock()
        result = select_revalidation_candidates(store, mode="cadence", limit=0)
        assert result.selected == []
        assert result.considered == 0
