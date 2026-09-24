"""F-21: steward proposal resolutions record who resolved them.

Review addendum F-21: ``curation_drain`` approved proposals through
``resolve_steward_proposal``, which always wrote ``source: human_override`` and
``steward_human_override:approve`` -- 340 automatic approvals were recorded as
operator corrections, unusable as audit or RL ground truth.  Resolutions now
carry an explicit ``actor`` (``operator`` | ``automation``); the drain passes
``automation``; and proposals whose payload says ``source: jev`` are never
auto-approved (they wait for the operator).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import curation_drain
from memorymaster.govern.steward import list_steward_proposals, resolve_steward_proposal


@pytest.fixture()
def svc(tmp_path: Path) -> MemoryService:
    service = MemoryService(tmp_path / "actor.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _ingest(svc: MemoryService, text: str, **kw):
    return svc.ingest(text=text, citations=[CitationInput(source="test", locator="loc")],
                      source_agent="test-agent", **kw)


def _confirm(svc: MemoryService, claim_id: int) -> None:
    claim = svc.store.get_claim(claim_id, include_citations=False)
    svc.store.apply_status_transition(claim, to_status="confirmed", reason="test", event_type="validator")


def _supersession_proposal(svc: MemoryService, text: str) -> tuple[int, int]:
    target = _ingest(svc, f"the original {text}")
    _confirm(svc, target.id)
    newer = _ingest(svc, f"the corrected {text}", supersedes_claim_id=target.id)
    _confirm(svc, newer.id)
    for event in svc.list_events(event_type="policy_decision", limit=200):
        if event.details == "steward_proposal:superseded_candidate" and event.claim_id == target.id:
            return target.id, int(event.id)
    raise AssertionError("fixture produced no supersession proposal")


def _jev_proposal(svc: MemoryService) -> tuple[int, int]:
    claim = _ingest(svc, "a claim that jev thinks duplicates another")
    _confirm(svc, claim.id)
    event = svc.store.record_event(
        claim_id=claim.id, event_type="policy_decision", from_status="confirmed",
        to_status="stale", details="steward_proposal:stale",
        payload={"source": "jev", "decision": "stale", "proposed_status": "stale",
                 "reasons": [{"code": "jev_dedup"}]},
    )
    event_id = getattr(event, "id", event)
    if not isinstance(event_id, int):
        event_id = next(
            int(e.id) for e in svc.list_events(event_type="policy_decision", limit=50)
            if e.claim_id == claim.id and e.details == "steward_proposal:stale"
        )
    return claim.id, event_id


def _resolution_payload(svc: MemoryService, proposal_event_id: int) -> dict:
    for event in svc.list_events(event_type="audit", limit=200):
        payload = json.loads(event.payload_json or "{}")
        if payload.get("proposal_event_id") == proposal_event_id:
            return payload
    raise AssertionError("no resolution audit event")


def _transition_reasons(svc: MemoryService, claim_id: int) -> list[str]:
    return [str(e.details or "") for e in svc.list_events(claim_id=claim_id, limit=50)]


def test_automation_actor_is_recorded_and_not_labelled_human(svc):
    target, proposal = _supersession_proposal(svc, "measurement of free space")

    result = resolve_steward_proposal(svc, action="approve", proposal_event_id=proposal,
                                      actor="automation")

    assert result["resolved"] is True
    payload = _resolution_payload(svc, proposal)
    assert payload["actor"] == "automation"
    assert payload["source"] != "human_override"
    assert not any("human_override" in reason for reason in _transition_reasons(svc, target))


def test_operator_remains_the_default_actor(svc):
    _, proposal = _supersession_proposal(svc, "boiling point")

    resolve_steward_proposal(svc, action="reject", proposal_event_id=proposal)

    payload = _resolution_payload(svc, proposal)
    assert payload["actor"] == "operator"
    assert payload["source"] == "human_override"


def test_unknown_actor_is_rejected(svc):
    _, proposal = _supersession_proposal(svc, "melting point")
    with pytest.raises(ValueError, match="actor"):
        resolve_steward_proposal(svc, action="approve", proposal_event_id=proposal, actor="bot")


def test_automation_cannot_resolve_a_jev_proposal(svc):
    claim_id, proposal = _jev_proposal(svc)
    with pytest.raises(ValueError, match="jev"):
        resolve_steward_proposal(svc, action="approve", proposal_event_id=proposal, actor="automation")
    assert svc.store.get_claim(claim_id, include_citations=False).status == "confirmed"


def test_curation_drain_resolves_as_automation(svc):
    _, proposal = _supersession_proposal(svc, "speed of sound")

    summary = curation_drain.drain_proposals(svc, apply=True)

    assert summary["approved"] == 1
    assert _resolution_payload(svc, proposal)["actor"] == "automation"


def test_curation_drain_never_auto_approves_jev_proposals(svc):
    claim_id, proposal = _jev_proposal(svc)

    summary = curation_drain.drain_proposals(svc, apply=True)

    assert summary["approved"] == 0
    assert summary["kept_jev"] == 1
    assert svc.store.get_claim(claim_id, include_citations=False).status == "confirmed"
    pending = {p["proposal_event_id"] for p in list_steward_proposals(svc, limit=50)}
    assert proposal in pending, "the jev proposal must stay in the operator queue"


def test_pending_jev_proposals_do_not_starve_the_drain_limit(svc):
    """Verifier note: jev proposals are kept for the operator, so they must not
    use up ``drain_proposals``' limit and block the proposals it may resolve."""
    jev_before = [_jev_proposal(svc)[1] for _ in range(2)]
    _, proposal = _supersession_proposal(svc, "density of water")
    jev_after = [_jev_proposal(svc)[1] for _ in range(2)]

    summary = curation_drain.drain_proposals(svc, limit=1, apply=True)

    assert summary["approved"] == 1
    # Listing is newest first and stops once ``limit`` countable proposals
    # are found, so the jev rows older than the resolved one are not scanned.
    assert summary["kept_jev"] >= 2
    assert _resolution_payload(svc, proposal)["actor"] == "automation"
    pending = {p["proposal_event_id"] for p in list_steward_proposals(svc, limit=50)}
    assert set(jev_before + jev_after) <= pending


def _lifecycle_payloads(svc: MemoryService, claim_id: int, event_type: str, reason: str) -> list[dict]:
    return [
        json.loads(event.payload_json or "{}")
        for event in svc.list_events(claim_id=claim_id, limit=50)
        if event.event_type == event_type and str(event.details or "").startswith(reason)
    ]


def test_the_supersession_event_payload_carries_the_actor(svc):
    target, proposal = _supersession_proposal(svc, "orbital period")

    resolve_steward_proposal(svc, action="approve", proposal_event_id=proposal, actor="automation")

    [payload] = _lifecycle_payloads(svc, target, "supersession", "steward_automation:approve")
    assert payload["actor"] == "automation"
    assert isinstance(payload["replaced_by_claim_id"], int)


def test_the_transition_event_payload_carries_the_actor(svc):
    claim_id, proposal = _jev_proposal(svc)

    resolve_steward_proposal(svc, action="approve", proposal_event_id=proposal)

    assert svc.store.get_claim(claim_id, include_citations=False).status == "stale"
    [payload] = _lifecycle_payloads(svc, claim_id, "transition", "steward_human_override:approve")
    assert payload == {"actor": "operator"}



def _steward_apply(svc: MemoryService, tmp_path: Path) -> None:
    from memorymaster.govern.steward import run_steward

    run_steward(svc, mode="manual", max_cycles=1, max_claims=10, max_proposals=10,
                max_probe_files=10, apply=True, artifact_path=tmp_path / "steward.json")


def test_a_steward_cycle_stale_transition_payload_carries_the_automation_actor(svc, tmp_path):
    # Verifier note on F-21: the direct apply path wrote no actor at all.
    claim = svc.ingest(text="Support pager is +1-555-0100",
                       citations=[CitationInput(source="session://chat", locator="turn-1")],
                       subject="support", predicate="pager", object_value="+1-555-0100",
                       confidence=0.6, source_agent="test-agent")
    _confirm(svc, claim.id)

    _steward_apply(svc, tmp_path)

    assert svc.store.get_claim(claim.id, include_citations=False).status == "stale"
    [payload] = _lifecycle_payloads(svc, claim.id, "transition", "steward_apply:stale")
    assert payload == {"actor": "automation"}


def test_a_steward_cycle_supersession_payload_carries_the_automation_actor(svc):
    # Two confirmed claims on one tuple violate the confirmed-tuple index, so
    # drive the cycle's apply step directly with a superseded_candidate.
    from memorymaster.govern.steward import Decision, _apply_decision

    old = _ingest(svc, "the original escape velocity")
    _confirm(svc, old.id)
    new = _ingest(svc, "the corrected escape velocity")
    claim = svc.store.get_claim(old.id, include_citations=False)
    decision = Decision(claim_id=old.id, current_status="confirmed", decision="superseded_candidate",
                        proposed_status="superseded", reasons=[], proposal_priority=0.5,
                        replaced_by_claim_id=new.id)

    _apply_decision(svc, claim, decision)

    assert decision.applied is True
    [payload] = _lifecycle_payloads(svc, old.id, "supersession", "steward_apply:superseded_candidate")
    assert payload["actor"] == "automation"
    assert payload["replaced_by_claim_id"] == new.id


class _PgCursor:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, *args, **kwargs) -> None:
        return None

    def fetchall(self):
        return self.rows


class _PgConn(_PgCursor):
    def cursor(self):
        return _PgCursor(self.rows)


def _pg_store(rows=()):
    from types import SimpleNamespace

    from memorymaster.stores.postgres_store import PostgresStore

    store = PostgresStore.__new__(PostgresStore)
    payloads: list[dict | None] = []
    store.connect = lambda: _PgConn(rows)
    store._insert_event_row = lambda conn, **kw: payloads.append(kw["payload"])
    store._update_superseded_claim = lambda *args: None
    store._update_replacement_claim = lambda *args: None
    claim = SimpleNamespace(id=1, status="confirmed", source_agent="agent", idempotency_key=None,
                            last_validated_at=None, replaced_by_claim_id=None)
    store.get_claim = lambda *args, **kwargs: claim
    return store, claim, payloads


def test_postgres_lifecycle_events_carry_the_extra_payload():
    store, claim, payloads = _pg_store()
    store.apply_status_transition(claim, to_status="stale", reason="r", event_type="transition",
                                  event_payload={"actor": "automation"})
    assert payloads == [{"actor": "automation"}]

    rows = [{"id": 1, "status": "confirmed", "version": 1, "replaced_by_claim_id": None,
             "supersedes_claim_id": None},
            {"id": 2, "status": "confirmed", "version": 1, "replaced_by_claim_id": None,
             "supersedes_claim_id": None}]
    store, _, payloads = _pg_store(rows)
    store.mark_superseded(1, 2, "r", event_payload={"actor": "operator"})
    assert payloads == [{"replaced_by_claim_id": 2, "actor": "operator"}]
