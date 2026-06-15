"""Coverage track T25 — extra lifecycle + _storage_lifecycle behavior.

Anchored on the lifecycle CONTRACT (not implementation details):

- `transition_claim`: a valid status change must land AND leave a durable audit
  event; same-status is a no-op (no write/event); an illegal transition refuses
  to corrupt the claim.
- `mark_superseded`: must set BOTH sides of the supersede pair (old.replaced_by
  + new.supersedes) and flip the old claim's status to 'superseded'.
- `record_event`: must persist exactly one events row carrying the right type
  and claim_id so downstream audits/wiki can find it.
- `record_access`: must increment access_count and stamp last_accessed, because
  tier recomputation and recall ordering depend on those signals.

All tests use a real tmp SQLite DB built from the project schema via
`SQLiteStore.init_db()`. No LLM/network/Postgres is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.stores.storage import SQLiteStore


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "memorymaster-lifecycle-extra.db")
    store.init_db()
    return store


def _cite(label: str = "evidence") -> CitationInput:
    return CitationInput(
        source="test://lifecycle-extra",
        locator=label,
        excerpt=f"{label} excerpt",
    )


@pytest.fixture(autouse=True)
def _no_wiki_autopromote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the validator wiki autopromote side-channel.

    transition_claim() can fire `wiki_engine.absorb_single_claim` after a
    validator event. That path is LLM/wiki territory — keep it off so these
    tests exercise pure lifecycle persistence and never hit external infra.
    """
    monkeypatch.setenv("MEMORYMASTER_WIKI_AUTOPROMOTE_THRESHOLD", "0")


# ---------------------------------------------------------------------------
# transition_claim
# ---------------------------------------------------------------------------


def test_valid_transition_lands_and_records_audit_event(tmp_path: Path) -> None:
    """A confirmed transition must persist the new status AND an audit event.

    Contract: lifecycle changes are auditable. If the status changes but no
    event is written (or vice-versa), the audit trail is broken — so we assert
    on BOTH the claim's new status and the recorded from/to event.
    """
    store = _store(tmp_path)
    claim = store.create_claim("candidate claim awaiting validation", [_cite("t1")])
    assert claim.status == "candidate"

    confirmed = transition_claim(
        store,
        claim.id,
        "confirmed",
        "validator accepted the claim",
        event_type="validator",
    )

    assert confirmed.status == "confirmed"
    assert confirmed.version == claim.version + 1

    reloaded = store.get_claim(claim.id, include_citations=False)
    assert reloaded is not None
    assert reloaded.status == "confirmed", "transition must be durably persisted"

    events = store.list_events(claim_id=claim.id, event_type="validator")
    assert len(events) == 1, "exactly one audit event should record the transition"
    assert events[0].from_status == "candidate"
    assert events[0].to_status == "confirmed"
    assert events[0].details == "validator accepted the claim"


def test_same_status_transition_is_noop_and_writes_no_event(tmp_path: Path) -> None:
    """Transitioning to the current status must not bump version or log an event.

    Contract: a no-op must leave zero footprint — no version bump, no spurious
    audit row. Otherwise idempotent retries would pollute the event chain.
    """
    store = _store(tmp_path)
    claim = store.create_claim("already-candidate claim", [_cite("noop")])

    result = transition_claim(store, claim.id, "candidate", "no real change")

    assert result.status == "candidate"
    assert result.version == claim.version, "no-op must not bump version"
    assert store.list_events(claim_id=claim.id, event_type="transition") == []


def test_invalid_transition_rejected_and_claim_unchanged(tmp_path: Path) -> None:
    """An illegal transition must raise AND leave the claim untouched.

    candidate -> stale is not in ALLOWED_TRANSITIONS. The contract is that the
    refusal is total: no status change, no version bump, no event. A partial
    write here would silently corrupt the lifecycle invariant.
    """
    store = _store(tmp_path)
    claim = store.create_claim("candidate that may not skip to stale", [_cite("bad")])

    with pytest.raises(ValueError, match="Invalid transition"):
        transition_claim(store, claim.id, "stale", "illegal jump")

    reloaded = store.get_claim(claim.id, include_citations=False)
    assert reloaded is not None
    assert reloaded.status == "candidate"
    assert reloaded.version == claim.version
    # No transition event was logged (only the create-time 'ingest' event remains).
    assert store.list_events(claim_id=claim.id, event_type="transition") == []


def test_superseded_transition_requires_replacement_id(tmp_path: Path) -> None:
    """Transitioning to 'superseded' without a replacement id must be refused.

    Contract: a superseded claim must always point at what replaced it. Allowing
    a dangling supersede would break wiki/audit reconciliation.
    """
    store = _store(tmp_path)
    claim = store.create_claim("claim heading to superseded", [_cite("sup")])

    with pytest.raises(ValueError, match="replaced_by_claim_id"):
        transition_claim(store, claim.id, "superseded", "missing replacement")

    reloaded = store.get_claim(claim.id, include_citations=False)
    assert reloaded is not None
    assert reloaded.status == "candidate", "rejected transition must not mutate status"


def test_transition_nonexistent_claim_raises(tmp_path: Path) -> None:
    """Transitioning a claim that does not exist must raise, not silently no-op."""
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="does not exist"):
        transition_claim(store, 999999, "confirmed", "ghost", event_type="validator")


# ---------------------------------------------------------------------------
# mark_superseded
# ---------------------------------------------------------------------------


def test_mark_superseded_sets_both_sides_of_pair_and_status(tmp_path: Path) -> None:
    """mark_superseded must wire BOTH directions of the supersede pair.

    Contract (claims-lifecycle rule): set replaced_by_claim_id on the OLD claim
    AND supersedes_claim_id on the NEW claim, and flip the old claim to
    'superseded'. A one-sided link breaks the wiki and integrity reconciler, so
    we assert on every leg of the pair plus the status and the supersession
    audit event.
    """
    store = _store(tmp_path)
    old = store.create_claim("the old truth", [_cite("old")])
    new = store.create_claim("the newer truth", [_cite("new")])

    store.mark_superseded(old.id, new.id, "newer evidence replaces it")

    reloaded_old = store.get_claim(old.id, include_citations=False)
    reloaded_new = store.get_claim(new.id, include_citations=False)
    assert reloaded_old is not None and reloaded_new is not None

    assert reloaded_old.status == "superseded"
    assert reloaded_old.replaced_by_claim_id == new.id, "old must point forward"
    assert reloaded_new.supersedes_claim_id == old.id, "new must point back"
    assert reloaded_old.valid_until is not None, "superseded claim is no longer current"

    events = store.list_events(claim_id=old.id, event_type="supersession")
    assert len(events) == 1
    assert events[0].to_status == "superseded"


def test_mark_superseded_double_supersede_is_refused(tmp_path: Path) -> None:
    """Superseding an already-superseded claim must raise, not double-write.

    Contract: the supersede pair is established exactly once. A second attempt
    is a concurrency/consistency error and must not overwrite the existing link.
    """
    from memorymaster.stores._storage_shared import ConcurrentModificationError

    store = _store(tmp_path)
    old = store.create_claim("old truth", [_cite("old")])
    first_new = store.create_claim("first replacement", [_cite("n1")])
    second_new = store.create_claim("second replacement", [_cite("n2")])

    store.mark_superseded(old.id, first_new.id, "first supersede")

    with pytest.raises(ConcurrentModificationError):
        store.mark_superseded(old.id, second_new.id, "second supersede attempt")

    reloaded_old = store.get_claim(old.id, include_citations=False)
    assert reloaded_old is not None
    assert reloaded_old.replaced_by_claim_id == first_new.id, "original link preserved"


# ---------------------------------------------------------------------------
# record_event
# ---------------------------------------------------------------------------


def test_record_event_persists_row_with_type_and_claim_id(tmp_path: Path) -> None:
    """record_event must write exactly one events row with the given type + claim_id.

    Contract: events are the audit substrate. A recorded event must be findable
    by its claim_id and carry the exact event_type passed in — otherwise audit
    queries and the wiki autopromote counter read the wrong history.
    """
    store = _store(tmp_path)
    claim = store.create_claim("claim that will decay", [_cite("ev")])

    store.record_event(
        claim_id=claim.id,
        event_type="decay",
        from_status="confirmed",
        to_status="stale",
        details="freshness window elapsed",
    )

    events = store.list_events(claim_id=claim.id, event_type="decay")
    assert len(events) == 1, "exactly one decay event must be persisted"
    event = events[0]
    assert event.claim_id == claim.id
    assert event.event_type == "decay"
    assert event.from_status == "confirmed"
    assert event.to_status == "stale"
    assert event.details == "freshness window elapsed"


def test_record_event_rejects_unknown_event_type(tmp_path: Path) -> None:
    """record_event must reject event types outside the validated allow-list.

    Contract: only known EVENT_TYPES may enter the audit log. Accepting an
    arbitrary string would let typos silently fragment audit history.
    """
    store = _store(tmp_path)
    claim = store.create_claim("claim with a typo event", [_cite("ev2")])

    before = len(store.list_events(claim_id=claim.id))

    with pytest.raises(ValueError, match="Invalid event_type"):
        store.record_event(claim_id=claim.id, event_type="not_a_real_event")

    after = len(store.list_events(claim_id=claim.id))
    assert after == before, "rejected type must not append any new event row"


# ---------------------------------------------------------------------------
# record_access
# ---------------------------------------------------------------------------


def test_record_access_increments_count_and_stamps_last_accessed(tmp_path: Path) -> None:
    """record_access must increment access_count and set last_accessed each call.

    Contract: recall ordering and tier recomputation read access_count /
    last_accessed. If access doesn't accumulate, hot claims never get promoted
    to 'core'. We assert the count climbs by one per call and the timestamp is
    populated.
    """
    store = _store(tmp_path)
    claim = store.create_claim("frequently recalled claim", [_cite("acc")])
    assert claim.access_count == 0
    assert claim.last_accessed is None

    store.record_access(claim.id)
    after_one = store.get_claim(claim.id, include_citations=False)
    assert after_one is not None
    assert after_one.access_count == 1, "first access must increment to 1"
    assert after_one.last_accessed is not None, "last_accessed must be stamped"

    store.record_access(claim.id)
    after_two = store.get_claim(claim.id, include_citations=False)
    assert after_two is not None
    assert after_two.access_count == 2, "each access increments the counter"


def test_record_accesses_batch_increments_each_listed_claim(tmp_path: Path) -> None:
    """Batch access must increment exactly the listed claims, once each.

    Contract: the batch path is an optimization of record_access and must stay
    behaviorally identical — every targeted claim gains one access; untargeted
    claims are left at zero.
    """
    store = _store(tmp_path)
    a = store.create_claim("batch claim a", [_cite("a")])
    b = store.create_claim("batch claim b", [_cite("b")])
    untouched = store.create_claim("not in the batch", [_cite("c")])

    store.record_accesses_batch([a.id, b.id])

    reloaded_a = store.get_claim(a.id, include_citations=False)
    reloaded_b = store.get_claim(b.id, include_citations=False)
    reloaded_untouched = store.get_claim(untouched.id, include_citations=False)
    assert reloaded_a is not None and reloaded_b is not None
    assert reloaded_untouched is not None

    assert reloaded_a.access_count == 1
    assert reloaded_b.access_count == 1
    assert reloaded_untouched.access_count == 0, "non-batched claim untouched"
