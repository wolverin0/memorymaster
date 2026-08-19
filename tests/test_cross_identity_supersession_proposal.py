"""A correction from another agent must be recorded, not thrown away.

``resolve_supersession_target`` required the replacement and its target to match
on scope, visibility and source_agent, and raised otherwise — which failed the
whole ingest, so the correction vanished into a VALIDATION_ERROR.

That check protected nothing. Same-identity supersession does not retire the
target either: it files ``steward_proposal:superseded_candidate`` and a human
decides. Nobody can unilaterally retire another agent's claim, with or without
the boundary. All the boundary decided was whether a correction got *recorded*
or *discarded*.

The case that prompted this (pane-0, 2026-08-19): a false claim at scope
``global`` from ``dream-worker`` could not be corrected by any session agent,
because ``source_agent`` can never match. On the production database 133
confirmed claims live at scope ``global`` and 26 came from that worker — a class
of claims with no correction path at all.

Cross-identity now files the same proposal, flagged so a reviewer can see the
correction came from elsewhere. The human gate is unchanged. Note what this does
NOT promise: the steward queue had 220 unresolved proposals when this was
written, the oldest from 2026-04-22. Recording a correction makes it traceable;
it does not make it attended to.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


@pytest.fixture()
def service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "cross.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _ingest(svc: MemoryService, text: str, **kw):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="test", locator="l", excerpt="e")],
        **kw,
    )


def _proposals(svc: MemoryService, claim_id: int):
    return [
        event
        for event in svc.list_events(
            claim_id=claim_id, event_type="policy_decision", limit=20
        )
        if event.details == "steward_proposal:superseded_candidate"
    ]


def test_cross_agent_correction_is_recorded_instead_of_rejected(service):
    """The reported case: another agent's claim can now be corrected."""
    false_claim = _ingest(
        service,
        "T-0041 says verbatim: MOVE RESTORE VERIFICATION OFF-HOST.",
        scope="global",
        source_agent="dream-worker",
    )

    correction = _ingest(
        service,
        "T-0041 says no such thing; verified against the task file.",
        scope="project:test",
        source_agent="claude-session",
        supersedes_claim_id=false_claim.id,
    )

    assert correction.id, "the correction must be ingested, not rejected"
    assert _proposals(service, false_claim.id), (
        "no steward proposal was filed — the correction was discarded"
    )


def test_the_proposal_says_it_came_from_another_identity(service):
    """A reviewer must be able to tell this crossed an identity boundary."""
    target = _ingest(service, "Original.", scope="global", source_agent="dream-worker")
    replacement = _ingest(
        service,
        "Correction from elsewhere.",
        scope="project:test",
        source_agent="claude-session",
        supersedes_claim_id=target.id,
    )

    payload = json.loads(_proposals(service, target.id)[0].payload_json)
    assert payload["cross_identity"] is True
    assert payload["replaced_by_claim_id"] == replacement.id
    codes = {reason["code"] for reason in payload["reasons"]}
    assert "cross_identity_supersession" in codes


def test_the_target_is_not_modified(service):
    """Recording a proposal must not retire anything on its own."""
    target = _ingest(service, "Original.", scope="global", source_agent="dream-worker")
    service.store.apply_status_transition(
        target, to_status="confirmed", reason="fixture", event_type="validator"
    )

    _ingest(
        service,
        "Correction from elsewhere.",
        scope="project:test",
        source_agent="claude-session",
        supersedes_claim_id=target.id,
    )

    assert service.store.get_claim(target.id).status == "confirmed"


def test_same_identity_proposal_is_not_flagged_as_crossing(service):
    """The existing path must keep behaving exactly as before."""
    target = _ingest(service, "Original.", scope="project:test", source_agent="a")
    _ingest(
        service,
        "Replacement.",
        scope="project:test",
        source_agent="a",
        supersedes_claim_id=target.id,
    )

    payload = json.loads(_proposals(service, target.id)[0].payload_json)
    assert payload.get("cross_identity") is False


def test_a_target_that_does_not_exist_still_raises(service):
    """Relaxing the identity check must not relax existence."""
    with pytest.raises(ValueError, match="does not exist"):
        _ingest(service, "Replacement.", supersedes_claim_id=999999)


def test_an_inactive_target_still_raises(service):
    """An archived claim is not a supersession candidate, whoever asks."""
    target = _ingest(service, "Original.", scope="global", source_agent="dream-worker")
    service.store.apply_status_transition(
        target, to_status="archived", reason="fixture", event_type="validator"
    )

    with pytest.raises(ValueError, match="not active"):
        _ingest(
            service,
            "Correction.",
            scope="project:test",
            source_agent="claude-session",
            supersedes_claim_id=target.id,
        )


def test_one_proposal_per_target_still_holds_across_identities(service):
    """Dedup is per target, so this cannot be used to flood the queue."""
    target = _ingest(service, "Original.", scope="global", source_agent="dream-worker")
    for i in range(3):
        _ingest(
            service,
            f"Correction attempt {i}.",
            scope="project:test",
            source_agent="claude-session",
            supersedes_claim_id=target.id,
        )

    assert len(_proposals(service, target.id)) == 1
