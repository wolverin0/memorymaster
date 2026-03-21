"""Tests for memorymaster.lifecycle — state transition logic."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from memorymaster.lifecycle import ALLOWED_TRANSITIONS, can_transition, transition_claim


class TestCanTransition:
    def test_allowed(self):
        assert can_transition("candidate", "confirmed") is True
        assert can_transition("confirmed", "stale") is True
        assert can_transition("stale", "archived") is True

    def test_disallowed(self):
        assert can_transition("archived", "confirmed") is False
        assert can_transition("candidate", "stale") is False

    def test_unknown_status(self):
        assert can_transition("nonexistent", "confirmed") is False

    def test_self_transition_disallowed(self):
        for status in ALLOWED_TRANSITIONS:
            assert can_transition(status, status) is False


class TestTransitionClaim:
    def _mock_store(self, claim_status="candidate"):
        store = MagicMock()
        claim = MagicMock()
        claim.id = 1
        claim.status = claim_status
        store.get_claim.return_value = claim
        store.apply_status_transition.return_value = claim
        return store, claim

    def test_nonexistent_claim_raises(self):
        store = MagicMock()
        store.get_claim.return_value = None
        with pytest.raises(ValueError, match="does not exist"):
            transition_claim(store, claim_id=999, to_status="confirmed", reason="test")

    def test_same_status_returns_claim(self):
        store, claim = self._mock_store("confirmed")
        result = transition_claim(store, claim_id=1, to_status="confirmed", reason="noop")
        assert result is claim
        store.apply_status_transition.assert_not_called()

    def test_invalid_transition_raises(self):
        store, _ = self._mock_store("archived")
        with pytest.raises(ValueError, match="Invalid transition"):
            transition_claim(store, claim_id=1, to_status="confirmed", reason="bad")

    def test_superseded_without_replaced_by_raises(self):
        store, _ = self._mock_store("candidate")
        with pytest.raises(ValueError, match="replaced_by_claim_id"):
            transition_claim(store, claim_id=1, to_status="superseded", reason="test")

    def test_valid_transition_calls_store(self):
        store, claim = self._mock_store("candidate")
        transition_claim(store, claim_id=1, to_status="confirmed", reason="validated")
        store.apply_status_transition.assert_called_once()

    def test_superseded_with_replaced_by(self):
        store, claim = self._mock_store("candidate")
        transition_claim(
            store, claim_id=1, to_status="superseded",
            reason="newer claim", replaced_by_claim_id=2,
        )
        store.apply_status_transition.assert_called_once()
        kwargs = store.apply_status_transition.call_args
        assert kwargs[1]["replaced_by_claim_id"] == 2
