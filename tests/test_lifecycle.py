"""Tests for memorymaster.core.lifecycle — state transition logic."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from memorymaster.core.lifecycle import ALLOWED_TRANSITIONS, can_transition, transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.promotion_policy import PromotionPolicyBlocked
from memorymaster.core.service import MemoryService


@pytest.mark.parametrize("fail_confirmation", [False, True])
def test_confirmation_and_tuple_retirement_are_atomic(tmp_path, monkeypatch, fail_confirmation):
    service = MemoryService(str(tmp_path / "lifecycle.db"), workspace_root=str(tmp_path))
    service.init_db()
    old, new = [
        service.ingest(
            text=f"The diagnosis is {value}.", subject="diagnosis", predicate="cause",
            object_value=value, scope="project:test", source_agent="claude-session",
            citations=[CitationInput(source=f"fixture:{value}")],
        )
        for value in ("old", "new")
    ]
    old = transition_claim(service.store, old.id, "confirmed", reason="fixture")
    assert new.supersedes_claim_id is None
    before_events = service.store.list_events(limit=100)
    insert_event = service.store._insert_event_row
    confirmation_seen = []

    def observe_confirmation(conn, **kwargs):
        if kwargs.get("claim_id") == new.id and kwargs.get("to_status") == "confirmed":
            assert conn.in_transaction
            predecessor = conn.execute(
                "SELECT status, replaced_by_claim_id FROM claims WHERE id = ?", (old.id,),
            ).fetchone()
            replacement = conn.execute(
                "SELECT status, supersedes_claim_id FROM claims WHERE id = ?", (new.id,),
            ).fetchone()
            assert tuple(predecessor) == ("superseded", new.id)
            assert tuple(replacement) == ("confirmed", old.id)
            # A separate reader must still see the pre-transaction state.
            assert service.store.get_claim(old.id) == old
            assert service.store.get_claim(new.id) == new
            confirmation_seen.append(True)
            if fail_confirmation:
                raise sqlite3.OperationalError("injected confirmation failure")
        return insert_event(conn, **kwargs)

    monkeypatch.setattr(service.store, "_insert_event_row", observe_confirmation)

    def confirm():
        return transition_claim(
            service.store, new.id, "confirmed", reason="replacement accepted",
            supersedes_claim_id=old.id,
        )

    if fail_confirmation:
        with pytest.raises(sqlite3.OperationalError, match="injected confirmation failure"):
            confirm()
        assert service.store.get_claim(old.id) == old
        assert service.store.get_claim(new.id) == new
        assert service.store.list_events(limit=100) == before_events
    else:
        confirmed = confirm()
        retired = service.store.get_claim(old.id)
        assert confirmed.status == "confirmed"
        assert confirmed.supersedes_claim_id == old.id
        assert retired.status == "superseded"
        assert retired.replaced_by_claim_id == new.id
        assert retired.valid_until is not None
        events = service.store.list_events(limit=100)
        added = [event for event in events if event.id not in {e.id for e in before_events}]
        assert {(event.claim_id, event.to_status) for event in added} == {
            (old.id, "superseded"), (new.id, "confirmed"),
        }
        assert len(added) == 2
    assert confirmation_seen == [True]


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

    def test_non_auto_promotable_author_cannot_be_confirmed_automatically(self):
        store, claim = self._mock_store("candidate")
        claim.source_agent = "llm-stop-hook"
        with pytest.raises(PromotionPolicyBlocked):
            transition_claim(store, claim_id=1, to_status="confirmed", reason="validated")
        store.apply_status_transition.assert_not_called()

    def test_non_auto_promotable_author_confirms_with_explicit_actor(self):
        store, claim = self._mock_store("candidate")
        claim.source_agent = "llm-stop-hook"
        transition_claim(
            store, claim_id=1, to_status="confirmed", reason="operator",
            event_payload={"actor": "operator"},
        )
        store.apply_status_transition.assert_called_once()

    def test_non_auto_promotable_author_can_still_leave_confirmed(self):
        store, claim = self._mock_store("confirmed")
        claim.source_agent = "llm-stop-hook"
        transition_claim(store, claim_id=1, to_status="stale", reason="decay")
        store.apply_status_transition.assert_called_once()

    def test_predicate_without_subject_is_not_auto_promoted(self):
        store, claim = self._mock_store("candidate")
        claim.source_agent = "claude-session"
        claim.subject, claim.predicate = None, "CONSTRAINT"
        with pytest.raises(PromotionPolicyBlocked):
            transition_claim(store, claim_id=1, to_status="confirmed", reason="auto")
        store.apply_status_transition.assert_not_called()

    def test_predicate_without_subject_confirms_with_explicit_actor(self):
        store, claim = self._mock_store("candidate")
        claim.source_agent = "claude-session"
        claim.subject, claim.predicate = None, "CONSTRAINT"
        transition_claim(store, claim_id=1, to_status="confirmed", reason="operator",
                         event_payload={"actor": "operator"})
        store.apply_status_transition.assert_called_once()

    def test_subject_and_predicate_without_object_is_auto_promoted(self):
        store, claim = self._mock_store("candidate")
        claim.source_agent = "dream-worker"
        claim.subject, claim.predicate, claim.object_value = "ledger", "refuses old cards", None
        transition_claim(store, claim_id=1, to_status="confirmed", reason="auto")
        store.apply_status_transition.assert_called_once()
