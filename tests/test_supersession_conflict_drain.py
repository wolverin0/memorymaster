"""T-1656: an explicit predecessor must not defeat its own replacement."""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.promotion_policy import PromotionPolicyBlocked
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import curation_drain, validator
from memorymaster.stores._storage_shared import ConcurrentModificationError


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(validator, "_classifier_promote_decision", lambda *_: None)
    svc = MemoryService(str(tmp_path / "supersession.db"), workspace_root=str(tmp_path))
    svc.init_db()
    return svc


def replacement_pair(service, *, source="claude-session", retired=False):
    old = service.ingest(
        text="The original diagnosis has been disproved.", subject="diagnosis",
        predicate="cause", object_value="old", scope="project:test", confidence=0.95,
        citations=[CitationInput(source="fixture:old")], source_agent="claude-session",
    )
    transition_claim(service.store, old.id, "confirmed", reason="fixture")
    new = service.ingest(
        text="The corrected root cause is supported by two independent observations. " * 4,
        subject="diagnosis", predicate="cause", object_value="new", scope="project:test",
        confidence=0.84, source_agent=source, supersedes_claim_id=old.id,
        citations=[CitationInput(source="fixture:a"), CitationInput(source="fixture:b")],
    )
    service.store.mark_superseded(old.id, new.id, reason="fixture resolved supersession")
    if not retired:
        # Reproduce the historical unretired predecessor through the store API.
        # Keep the replacement's persisted link, but restore the old snapshot
        # with its current version so the validator must retire it again.
        service.store.apply_status_transition(
            replace(old, version=service.store.get_claim(old.id).version),
            to_status="confirmed", reason="fixture historical unretired predecessor",
            event_type="transition",
        )
    return service.store.get_claim(old.id), service.store.get_claim(new.id)


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("source", ["claude-session", "llm-stop-hook"])
def test_validator_replacement_never_enters_conflict_drain(service, retired, source):
    old, new = replacement_pair(service, source=source, retired=retired)
    assert new.supersedes_claim_id == old.id
    assert old.confidence >= new.confidence
    result = validator.run(service.store)
    curation_drain.drain_conflicts(service, apply=True)
    expected = "candidate" if source == "llm-stop-hook" else "confirmed"
    assert service.store.get_claim(new.id).status == expected
    assert result["superseded"] == int(expected == "confirmed" and not retired)
    if expected == "confirmed" or retired:
        ancestor = service.store.get_claim(old.id)
        assert ancestor.status == "superseded"
        assert ancestor.replaced_by_claim_id == new.id
    assert not any(e.to_status in {"conflicted", "stale"}
                   for e in service.store.list_events(claim_id=new.id, limit=100))


def test_orphaned_supersession_is_held_for_revalidation(service):
    _, new = replacement_pair(service, retired=True)
    transition_claim(service.store, new.id, "conflicted", reason="historical validator bug")
    result = curation_drain.drain_conflicts(service, apply=True)
    assert service.store.get_claim(new.id).status == "conflicted"
    assert result["pending_revalidation"] == 1
    validator.run(service.store, limit=0, revalidation_claims=[service.store.get_claim(new.id)],
                  policy_mode="revalidation")
    assert service.store.get_claim(new.id).status == "confirmed"


def test_failed_confirmation_rolls_back_ancestor_retirement(service, monkeypatch):
    old, new = replacement_pair(service)
    insert = service.store._insert_event_row

    def fail_confirmation(conn, **kwargs):
        if kwargs.get("claim_id") == new.id and kwargs.get("to_status") == "confirmed":
            raise sqlite3.OperationalError("injected confirmation failure")
        return insert(conn, **kwargs)

    monkeypatch.setattr(service.store, "_insert_event_row", fail_confirmation)
    with pytest.raises(sqlite3.OperationalError):
        transition_claim(service.store, new.id, "confirmed", reason="fixture",
                         supersedes_claim_id=old.id)
    assert service.store.get_claim(old.id).status == "confirmed"
    assert service.store.get_claim(old.id).replaced_by_claim_id is None
    assert service.store.get_claim(new.id).status == "candidate"


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "other"), ("scope", "project:other"),
    ("visibility", "private"), ("supersedes_claim_id", 99999),
])
def test_atomic_confirmation_rejects_other_identity_or_existing_link(service, field, value):
    old, new = replacement_pair(service)
    # Exercise the storage snapshot checks without persisting malformed fixtures.
    altered = replace(service.store.get_claim(new.id), **{field: value})
    with pytest.raises(ValueError, match="predecessor"):
        service.store.apply_status_transition(
            altered, to_status="confirmed", reason="fixture", event_type="validator",
            supersedes_claim_id=old.id,
        )
    assert service.store.get_claim(old.id).status == "confirmed"
    assert service.store.get_claim(new.id).status == "candidate"


def test_pinned_predecessor_cannot_be_automatically_superseded(service):
    old, new = replacement_pair(service)
    service.pin(old.id, True)
    validator.run(service.store)
    assert service.store.get_claim(old.id).status == "confirmed"
    assert service.store.get_claim(old.id).replaced_by_claim_id is None
    assert service.store.get_claim(new.id).status == "conflicted"


def test_atomic_supersession_cannot_bypass_source_policy(service):
    old, new = replacement_pair(service, source="llm-stop-hook")
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(service.store, new.id, "confirmed", reason="fixture",
                         supersedes_claim_id=old.id)
    assert service.store.get_claim(old.id).status == "confirmed"
    assert service.store.get_claim(new.id).status == "candidate"


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("injected storage failure"),
    ValueError("Dreaming confirmation requires a current source review"),
])
def test_validator_propagates_non_conflict_confirmation_failure(service, monkeypatch, retired, error):
    old, new = replacement_pair(service, retired=retired)
    apply = service.store.apply_status_transition

    def fail_confirmation(claim, **kwargs):
        if kwargs.get("to_status") == "confirmed":
            raise error
        return apply(claim, **kwargs)

    monkeypatch.setattr(service.store, "apply_status_transition", fail_confirmation)
    with pytest.raises(type(error), match=str(error)):
        validator.run(service.store)
    assert service.store.get_claim(new.id).status == "candidate"
    assert service.store.get_claim(old.id).status == ("superseded" if retired else "confirmed")


def test_drain_confirmation_failure_keeps_confirmed_rival(service, monkeypatch):
    old, new = replacement_pair(service)
    service.store.set_confidence(old.id, 0.3)
    transition_claim(service.store, new.id, "conflicted", reason="fixture")
    insert = service.store._insert_event_row

    def fail_confirmation(conn, **kwargs):
        if kwargs.get("claim_id") == new.id and kwargs.get("to_status") == "confirmed":
            raise sqlite3.OperationalError("injected drain confirmation failure")
        return insert(conn, **kwargs)

    monkeypatch.setattr(service.store, "_insert_event_row", fail_confirmation)
    result = curation_drain.drain_conflicts(service, apply=True)
    assert result["resolved_won"] == 0
    assert result["skipped_race"] == 1
    assert service.store.get_claim(old.id).status == "confirmed"
    assert service.store.get_claim(old.id).replaced_by_claim_id is None
    assert service.store.get_claim(new.id).status == "conflicted"
    assert service.store.get_claim(new.id).supersedes_claim_id == old.id


def test_explicit_predecessor_snapshot_is_confirmed_atomically(service):
    old, new = replacement_pair(service)
    service.store.apply_status_transition(
        service.store.get_claim(new.id), to_status="confirmed", reason="fixture", event_type="validator",
    )
    assert service.store.get_claim(new.id).status == "confirmed"
    assert service.store.get_claim(new.id).supersedes_claim_id == old.id
    assert service.store.get_claim(old.id).status == "superseded"
    assert service.store.get_claim(old.id).replaced_by_claim_id == new.id


def test_validator_preserves_different_superseded_ancestor(service):
    ancestor, new = replacement_pair(service, retired=True)
    rival = service.ingest(
        text="Another diagnosis occupies the tuple.", subject=new.subject,
        predicate=new.predicate, object_value="rival", scope=new.scope,
        confidence=0.3, citations=[CitationInput(source="fixture:rival")],
        source_agent="claude-session",
    )
    transition_claim(service.store, rival.id, "confirmed", reason="fixture")

    result = validator.run(service.store)

    assert result["conflicted"] == 1
    assert result["confirmed"] == result["superseded"] == 0
    assert service.store.get_claim(new.id).status == "conflicted"
    assert service.store.get_claim(new.id).supersedes_claim_id == ancestor.id
    assert service.store.get_claim(rival.id).status == "confirmed"
    assert service.store.get_claim(rival.id).replaced_by_claim_id is None


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("change", ["pinned", "stale", "superseded"])
def test_validator_holds_when_predecessor_changes_after_lookup(service, monkeypatch, explicit, change):
    if explicit:
        old, new = replacement_pair(service)
    else:
        old = service.ingest(
            text="The earlier diagnosis.", subject="diagnosis", predicate="cause",
            object_value="old", confidence=0.3, scope="project:test",
            citations=[CitationInput(source="fixture:old")], source_agent="claude-session",
        )
        transition_claim(service.store, old.id, "confirmed", reason="fixture")
        new = service.ingest(
            text="The corrected root cause has independent supporting evidence. " * 4,
            subject=old.subject, predicate=old.predicate, object_value="new", scope=old.scope,
            confidence=0.84, citations=[CitationInput(source="fixture:a"), CitationInput(source="fixture:b")],
            source_agent="claude-session",
        )
    lookup = service.store.find_confirmed_by_tuple

    def changed_after_lookup(**kwargs):
        related = lookup(**kwargs)
        if change == "pinned":
            service.pin(old.id, True)
        elif change == "stale":
            transition_claim(service.store, old.id, "stale", reason="concurrent writer")
        else:
            service.store.mark_superseded(old.id, new.id, reason="concurrent writer")
        return related

    monkeypatch.setattr(service.store, "find_confirmed_by_tuple", changed_after_lookup)
    result = validator.run(service.store)

    assert result["conflicted"] == 1
    assert result["confirmed"] == result["superseded"] == 0
    assert service.store.get_claim(new.id).status == "conflicted"
    assert service.store.get_claim(old.id).status == ("confirmed" if change == "pinned" else change)
    assert not any(e.to_status == "confirmed" for e in service.store.list_events(claim_id=new.id))


def test_validator_holds_on_concurrent_atomic_supersession_failure(service, monkeypatch):
    old, new = replacement_pair(service)
    apply = service.store.apply_status_transition

    def concurrent_confirmation(claim, **kwargs):
        if kwargs.get("to_status") == "confirmed":
            raise ConcurrentModificationError("Replacement already supersedes another claim.")
        return apply(claim, **kwargs)

    monkeypatch.setattr(service.store, "apply_status_transition", concurrent_confirmation)
    result = validator.run(service.store)

    assert result["conflicted"] == 1
    assert result["confirmed"] == result["superseded"] == 0
    assert service.store.get_claim(new.id).status == "conflicted"
    assert service.store.get_claim(old.id).status == "confirmed"


def test_dream_source_review_failure_preserves_predecessor(service):
    old, new = replacement_pair(service, source="dream-worker")
    with pytest.raises(ValueError, match="source review"):
        transition_claim(service.store, new.id, "confirmed", reason="fixture",
                         supersedes_claim_id=old.id)
    assert service.store.get_claim(old.id).status == "confirmed"
    assert service.store.get_claim(new.id).status == "candidate"


def test_postgres_atomic_confirmation_fails_before_opening_database():
    from memorymaster.stores.postgres_store import PostgresStore

    store = PostgresStore.__new__(PostgresStore)
    with pytest.raises(ValueError, match="SQLite-only"):
        store.apply_status_transition(
            None, to_status="confirmed", reason="fixture", event_type="validator",
            supersedes_claim_id=1,
        )
