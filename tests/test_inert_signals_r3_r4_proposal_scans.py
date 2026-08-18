"""R3 and R4 — the proposal queue must be read, and read completely.

R3: declaring `supersedes_claim_id` does not retire the old claim, it files a
`steward_proposal:superseded_candidate` for review. The validator never read
that proposal. It scored the claim on its own evidence and promoted it to
`confirmed` — so a claim somebody had explicitly declared outdated came back
stamped as verified truth, and recall serves confirmed claims. 21 of 59
production proposals were followed by exactly that promotion; claim 130542 sat
at `confirmed` for ~11 hours after being declared superseded.

R4: both readers of that queue bounded their scan by a global ROW cap, not by
time — `max(limit * 6, 500)` in the steward queue, `limit=2000` in recall.
`list_events` returns newest first, so the rows that fall off the cap are the
OLDEST: the proposals waiting longest for review are the first to disappear,
silently, while still unresolved. The steward side was at 362 of 600 in
production. The audit side fails the other way: when a RESOLUTION falls off,
an already-resolved proposal reads as pending again, and re-approving it lands
in the latch R2 fixed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.event_scan import PROPOSAL_SCAN_CEILING, scan_proposal_events
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import validator
from memorymaster.govern.steward import list_steward_proposals
from memorymaster.recall.retrieval import pending_supersession_ids

# The caps this test exists to defeat. Each flood is sized just past the cap
# that used to be in force, so the assertion fails on the old code and passes
# on the new one.
_OLD_PROPOSAL_CAP = 600  # steward: max(limit * 6, 500) at the default limit=100
_OLD_AUDIT_CAP = 800  # steward: max(limit * 8, 800)
_OLD_RECALL_CAP = 2000  # recall/retrieval.pending_supersession_ids


def _svc(path: Path) -> MemoryService:
    svc = MemoryService(path / "r3r4.db", workspace_root=path)
    svc.init_db()
    return svc


def _ingest(svc: MemoryService, text: str, **kw):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="test", locator="loc")],
        source_agent="test-agent",
        **kw,
    )


def _proposal_id_for(svc: MemoryService, claim_id: int) -> int:
    for event in svc.list_events(event_type="policy_decision", limit=5000):
        if event.details == "steward_proposal:superseded_candidate" and event.claim_id == claim_id:
            return int(event.id)
    raise AssertionError(f"no superseded_candidate proposal for claim {claim_id}")


def _flood(svc: MemoryService, event_type: str, count: int) -> None:
    """Bury the proposals under ordinary bookkeeping of the same event type.

    This is not a contrived load: `deterministic_adjust=+0.000` alone accounts
    for 487k of 2.4M production events, written ~15k/day.
    """
    for index in range(count):
        svc.store.record_event(
            claim_id=None,
            event_type=event_type,
            details="routine_bookkeeping",
            payload={"sequence": index},
        )


# --------------------------------------------------------------------------
# R3 — the validator must not promote a claim that is pending supersession
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_validator_does_not_promote_a_claim_pending_supersession(tmp_path: Path) -> None:
    """The whole point of filing the proposal is that this claim is doubted."""
    svc = _svc(tmp_path)
    outdated = _ingest(svc, "the free space figure measured before the cleanup ran")
    _ingest(
        svc,
        "the corrected free space figure measured after the cleanup ran",
        supersedes_claim_id=outdated.id,
    )
    assert outdated.id in pending_supersession_ids(svc, use_cache=False), "precondition"

    validator.run(svc.store, limit=50)

    after = svc.store.get_claim(outdated.id, include_citations=False)
    assert after.status != "confirmed", (
        "the validator promoted a claim someone had declared outdated; "
        "recall now serves it as verified truth"
    )
    blocked = [
        e for e in svc.list_events(claim_id=outdated.id, event_type="validator", limit=50)
        if e.details == "promotion_blocked_pending_supersession"
    ]
    assert blocked, "the block left no trace an operator could find"


@pytest.mark.unit
def test_validator_reports_what_it_blocked(tmp_path: Path) -> None:
    """A skip that is not counted is a skip nobody can notice."""
    svc = _svc(tmp_path)
    outdated = _ingest(
        svc,
        "an original measurement of the reclaimable disk space awaiting its correction",
    )
    _ingest(
        svc,
        "the correcting measurement of the reclaimable disk space taken after the fact",
        supersedes_claim_id=outdated.id,
    )

    result = validator.run(svc.store, limit=50)

    assert result["blocked_pending_supersession"] == 1
    # The replacement is not under a proposal and must still be promoted --
    # the gate is targeted, not a blanket halt of the cycle.
    assert result["confirmed"] == 1


@pytest.mark.unit
def test_validator_resumes_promotion_once_the_proposal_is_resolved(tmp_path: Path) -> None:
    """The gate must be a hold, not a permanent freeze — a rejected proposal
    releases the claim, otherwise this fix trades one silent failure for
    another."""
    svc = _svc(tmp_path)
    outdated = _ingest(svc, "a claim whose proposed supersession gets rejected")
    _ingest(svc, "the rejected replacement text", supersedes_claim_id=outdated.id)
    proposal_id = _proposal_id_for(svc, outdated.id)

    validator.run(svc.store, limit=50)
    assert svc.store.get_claim(outdated.id, include_citations=False).status == "candidate"

    svc.store.record_event(
        claim_id=outdated.id,
        event_type="audit",
        details="steward_proposal_rejected",
        payload={"proposal_event_id": proposal_id},
    )
    assert outdated.id not in pending_supersession_ids(svc, use_cache=False)

    validator.run(svc.store, limit=50)
    assert svc.store.get_claim(outdated.id, include_citations=False).status == "confirmed", (
        "a rejected proposal left the claim permanently unpromotable"
    )


@pytest.mark.unit
def test_the_validator_reads_a_proposal_filed_seconds_ago(tmp_path: Path) -> None:
    """Recall tolerates a five-minute-old view because it only shifts ranking.
    Promotion is a state change that outlives the cycle, so the validator must
    not run off a warm cache."""
    svc = _svc(tmp_path)
    outdated = _ingest(svc, "a claim superseded moments after the cache was warmed")

    # Warm the shared cache while nothing is pending — the state the recall
    # path leaves behind on any busy system.
    assert pending_supersession_ids(svc.store) == frozenset()

    _ingest(svc, "the very fresh correction", supersedes_claim_id=outdated.id)
    validator.run(svc.store, limit=50)

    assert svc.store.get_claim(outdated.id, include_citations=False).status != "confirmed", (
        "the validator promoted off a stale cache; a proposal filed seconds "
        "earlier had no effect for five minutes"
    )


# --------------------------------------------------------------------------
# R4 — the scan must be bounded by time, not by a row cap
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def proposal_flood(tmp_path_factory) -> tuple[MemoryService, int]:
    """A pending proposal buried under newer `policy_decision` events.

    Built once per module: each `record_event` is its own transaction.
    """
    svc = _svc(tmp_path_factory.mktemp("r4-proposals"))
    pending = _ingest(svc, "a measurement whose correction is still queued for review")
    _ingest(svc, "the queued correction", supersedes_claim_id=pending.id)
    _flood(svc, "policy_decision", _OLD_RECALL_CAP + 10)
    return svc, pending.id


@pytest.fixture(scope="module")
def audit_flood(tmp_path_factory) -> tuple[MemoryService, int]:
    """A RESOLVED proposal whose resolution is buried under newer `audit`
    events. Deliberately no `policy_decision` flood: the proposal itself must
    stay visible, or the test would pass for the wrong reason.
    """
    svc = _svc(tmp_path_factory.mktemp("r4-audit"))
    resolved = _ingest(svc, "a measurement whose proposed correction was rejected")
    _ingest(svc, "the rejected correction", supersedes_claim_id=resolved.id)
    svc.store.record_event(
        claim_id=resolved.id,
        event_type="audit",
        details="steward_proposal_rejected",
        payload={"proposal_event_id": _proposal_id_for(svc, resolved.id)},
    )
    _flood(svc, "audit", _OLD_RECALL_CAP + 10)
    return svc, resolved.id


@pytest.mark.unit
def test_a_pending_proposal_survives_an_event_flood(proposal_flood) -> None:
    """Past the row cap the proposal vanished from the operator queue while
    still unresolved — the oldest ones first, the ones most needing review."""
    svc, pending_id = proposal_flood
    queued = [p["claim_id"] for p in list_steward_proposals(svc, limit=100)]
    assert pending_id in queued, (
        f"an unresolved proposal fell out of the queue behind "
        f"{_OLD_PROPOSAL_CAP}+ newer events; nobody will ever see it again"
    )


@pytest.mark.unit
def test_the_recall_demotion_survives_an_event_flood(proposal_flood) -> None:
    """Losing the proposal here restores FULL score to the claim someone
    declared outdated, so it outranks its own correction again."""
    svc, pending_id = proposal_flood
    assert pending_id in pending_supersession_ids(svc, use_cache=False), (
        f"the demotion silently lifted behind {_OLD_RECALL_CAP}+ newer events"
    )


@pytest.mark.unit
def test_a_resolved_proposal_does_not_reappear_as_pending(audit_flood) -> None:
    """The mirror failure: when the RESOLUTION falls off the audit-side cap the
    proposal reads as pending again, and re-approving it is the latch R2
    fixed."""
    svc, resolved_id = audit_flood
    everything = list_steward_proposals(svc, limit=100, include_resolved=True)
    mine = [p for p in everything if p["claim_id"] == resolved_id]
    assert mine, "fixture is broken: the proposal itself is not in the scan"
    assert mine[0]["status"] == "rejected", (
        f"a resolved proposal came back as pending behind {_OLD_AUDIT_CAP}+ newer audit events"
    )
    pending_ids = [p["claim_id"] for p in list_steward_proposals(svc, limit=100)]
    assert resolved_id not in pending_ids
    assert resolved_id not in pending_supersession_ids(svc, use_cache=False), (
        f"recall re-applied a demotion that had been resolved, behind "
        f"{_OLD_RECALL_CAP}+ newer audit events"
    )


@pytest.mark.unit
def test_the_scan_says_when_it_hits_its_own_ceiling(proposal_flood, caplog) -> None:
    """The row ceiling that remains is a memory guard, not a window. Reaching
    it must be reported — that is the entire difference from the cap it
    replaced."""
    svc, _ = proposal_flood
    with caplog.at_level("WARNING"):
        clipped = scan_proposal_events(svc, ceiling=5)
    assert clipped.ceiling_hit is True
    assert "ceiling" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING"):
        full = scan_proposal_events(svc)
    assert full.ceiling_hit is False
    assert full.window_days > 0 and full.since
    assert len(full.proposals) < PROPOSAL_SCAN_CEILING
    assert "ceiling" not in caplog.text


@pytest.mark.unit
def test_a_broken_scan_is_not_reported_as_nothing_pending(tmp_path: Path) -> None:
    """`except Exception: ids = set()` made a fault indistinguishable from "no
    proposals" — and then cached that answer for five minutes. A fault must not
    outlive the call that hit it."""

    class _Flaky:
        def __init__(self, real):
            self._real = real
            self.fail = True

        def list_events(self, **kwargs):
            if self.fail:
                raise RuntimeError("events table unavailable")
            return self._real.list_events(**kwargs)

    svc = _svc(tmp_path)
    outdated = _ingest(svc, "a claim with a genuinely pending supersession")
    _ingest(svc, "its correction", supersedes_claim_id=outdated.id)

    flaky = _Flaky(svc.store)
    assert pending_supersession_ids(flaky) == frozenset()

    flaky.fail = False
    assert outdated.id in pending_supersession_ids(flaky), (
        "a transient fault was cached as 'nothing pending' and suppressed the "
        "demotion for the next five minutes"
    )
