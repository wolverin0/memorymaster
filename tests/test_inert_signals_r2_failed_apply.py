"""R2 — a failed apply must not masquerade as an approval.

WHY THIS EXISTS: `resolve_steward_proposal` recorded `steward_proposal_approved`
regardless of whether the apply succeeded. Three consumers read "an approved
event exists" as resolved and none read `payload["applied"]`, so a failed apply
landed in the worst reachable state at once:

  * the claim was never superseded — the outdated text stays live;
  * its ranking demotion was LIFTED (the proposal counts as resolved);
  * it left the operator queue, so nobody would ever look at it again;
  * and the `already_resolved` short-circuit REFUSED the retry.

Unrecoverable through the API. Observed for real: claim 123737 had two
dream-worker proposals filed one second apart with different replacements; the
first applied, the second failed with "already superseded" and latched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.steward import list_steward_proposals, resolve_steward_proposal
from memorymaster.recall.retrieval import pending_supersession_ids


def _svc(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "r2.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _ingest(svc: MemoryService, text: str, **kw):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="test", locator="loc")],
        source_agent="test-agent",
        **kw,
    )


def _confirm(svc: MemoryService, claim_id: int) -> None:
    claim = svc.store.get_claim(claim_id, include_citations=False)
    svc.store.apply_status_transition(
        claim, to_status="confirmed", reason="test", event_type="validator"
    )


def _proposal_for(svc: MemoryService, claim_id: int) -> int:
    for event in svc.list_events(event_type="policy_decision", limit=200):
        if event.details == "steward_proposal:superseded_candidate" and event.claim_id == claim_id:
            return int(event.id)
    raise AssertionError(f"no superseded_candidate proposal for claim {claim_id}")


def _build_doomed_approval(tmp_path: Path):
    """A target already retired, plus a live proposal against it: approving it
    can only fail. This is the production shape, minus the race."""
    svc = _svc(tmp_path)
    target = _ingest(svc, "the original measurement of free space")
    _confirm(svc, target.id)
    first = _ingest(svc, "the first correction of free space", supersedes_claim_id=target.id)
    _confirm(svc, first.id)
    proposal_id = _proposal_for(svc, target.id)

    # Retire the target out from under the pending proposal.
    resolve_steward_proposal(svc, action="approve", proposal_event_id=proposal_id)

    second = _ingest(svc, "a different, later correction of free space")
    _confirm(svc, second.id)
    return svc, target, second


@pytest.mark.unit
def test_a_failed_apply_is_reported_as_failed(tmp_path: Path) -> None:
    """The return must not claim resolution when nothing was applied."""
    svc, target, _ = _build_doomed_approval(tmp_path)
    from memorymaster.govern.ingest_governance import queue_supersession_proposal

    # Force a second proposal against the same (already superseded) target.
    svc.store.record_event(
        claim_id=target.id,
        event_type="policy_decision",
        details="steward_proposal:superseded_candidate",
        payload={"decision": "superseded_candidate", "proposed_status": "superseded",
                 "replaced_by_claim_id": 999_999},
    )
    doomed = [
        e.id for e in svc.list_events(event_type="policy_decision", limit=200)
        if e.details == "steward_proposal:superseded_candidate" and e.claim_id == target.id
    ][0]

    result = resolve_steward_proposal(svc, action="approve", proposal_event_id=int(doomed))

    assert result["applied"] is False
    assert result["resolved"] is False, "a failed apply reported itself as resolved"
    assert result["status"] == "apply_failed"
    assert queue_supersession_proposal is not None  # import kept meaningful


@pytest.mark.unit
def test_a_failed_apply_does_not_record_an_approval(tmp_path: Path) -> None:
    """The audit trail must not contain an approval that never took effect."""
    svc, target, _ = _build_doomed_approval(tmp_path)
    svc.store.record_event(
        claim_id=target.id,
        event_type="policy_decision",
        details="steward_proposal:superseded_candidate",
        payload={"decision": "superseded_candidate", "proposed_status": "superseded",
                 "replaced_by_claim_id": 999_999},
    )
    doomed = [
        e.id for e in svc.list_events(event_type="policy_decision", limit=200)
        if e.details == "steward_proposal:superseded_candidate" and e.claim_id == target.id
    ][0]
    resolve_steward_proposal(svc, action="approve", proposal_event_id=int(doomed))

    details = [str(e.details) for e in svc.list_events(event_type="audit", limit=100)]
    assert "steward_proposal_apply_failed" in details
    approvals = [
        e for e in svc.list_events(event_type="audit", limit=100)
        if str(e.details) == "steward_proposal_approved" and e.claim_id == target.id
    ]
    # Only the FIRST (genuine) approval may exist for this target.
    assert len(approvals) == 1


@pytest.mark.unit
def test_a_historical_failed_approval_does_not_lift_the_demotion(tmp_path: Path) -> None:
    """Rows written before this fix claim an approval that never applied. Both
    consumers must honour the row's own `applied: False` rather than the label."""
    svc = _svc(tmp_path)
    target = _ingest(svc, "an outdated measurement pending correction")
    _confirm(svc, target.id)
    _confirm(svc, _ingest(svc, "the correcting measurement", supersedes_claim_id=target.id).id)
    proposal_id = _proposal_for(svc, target.id)

    assert target.id in pending_supersession_ids(svc), "precondition: demotion is active"

    # The pre-fix shape: approved, but applied=False.
    svc.store.record_event(
        claim_id=target.id,
        event_type="audit",
        details="steward_proposal_approved",
        payload={"proposal_event_id": proposal_id, "applied": False,
                 "apply_error": "Claim was already superseded. Reload and retry."},
    )
    svc._pending_supersession_cache = None  # bypass the 5-min cache

    assert target.id in pending_supersession_ids(svc), (
        "a failed apply lifted the demotion, leaving the claim live AND unpenalised"
    )
    pending = [p for p in list_steward_proposals(svc, limit=100) if p.get("claim_id") == target.id]
    assert pending, "a failed apply removed the proposal from the operator queue"


@pytest.mark.unit
def test_only_one_supersession_proposal_per_target(tmp_path: Path) -> None:
    """The generator: dedup must be per TARGET. A second replacement against the
    same target can only ever fail, so it must never be queued."""
    svc = _svc(tmp_path)
    target = _ingest(svc, "a claim two different corrections will target")
    _confirm(svc, target.id)

    _confirm(svc, _ingest(svc, "correction number one", supersedes_claim_id=target.id).id)
    _confirm(svc, _ingest(svc, "correction number two", supersedes_claim_id=target.id).id)

    proposals = [
        e for e in svc.list_events(event_type="policy_decision", limit=200)
        if e.details == "steward_proposal:superseded_candidate" and e.claim_id == target.id
    ]
    assert len(proposals) == 1, f"{len(proposals)} proposals queued; all but one are doomed to fail"
