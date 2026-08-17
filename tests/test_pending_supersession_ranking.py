"""A proposed-but-unapplied supersession must demote the outdated claim.

WHY THIS EXISTS (infra audit 2026-08-17, pair mm-a72f / mm-a72f~2): a FALSE
claim ("fstrim reclaims the 48 GB") outranked its own correction, and neither
was flagged. Declaring ``supersedes_claim_id`` does not retire the old claim --
it files a ``steward_proposal:superseded_candidate`` for human review, and the
old claim keeps FULL score until someone approves it.

Review could not be relied on: **0 of 249 proposals had ever been resolved**,
the oldest pending ~4 months. So the correction lost to what it corrected, and
would have kept losing indefinitely. These tests pin the behaviour that fixes
it: the demotion lands when the supersession is PROPOSED.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall.retrieval import (
    _pending_supersession_penalty,
    _TIER_BONUS,
    pending_supersession_ids,
    rank_claim_rows,
)


def _svc(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "pending-supersession.db", workspace_root=tmp_path)
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


class _FakeClaim:
    """Minimal stand-in: the penalty only reads ``id``."""

    def __init__(self, claim_id: int) -> None:
        self.id = claim_id
        self.tier = "core"
        self.pinned = False


# ---------------------------------------------------------------------------
# The regression: correction must beat what it corrects, with no human step
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_correction_outranks_the_claim_it_supersedes(tmp_path: Path) -> None:
    """The mm-a72f case, end to end: declare the supersession at ingest and the
    outdated claim must stop winning -- without waiting for steward approval.

    Ranked through the weighted path, which is the one that orders by score
    (the `legacy` branch of rank_claim_rows returns unsorted; see
    ``test_legacy_path_scores_the_penalty_even_though_it_does_not_reorder``).
    """
    svc = _svc(tmp_path)
    false_claim = _ingest(svc, "fstrim reclaim datastore space fully", confidence=0.97)
    _confirm(svc, false_claim.id)

    correction = _ingest(
        svc,
        "fstrim reclaim datastore space is impossible without the controller primitive",
        confidence=0.95,
        supersedes_claim_id=false_claim.id,
    )
    _confirm(svc, correction.id)

    pending = pending_supersession_ids(svc)
    assert false_claim.id in pending, "the declaration must be on file as a proposal"

    claims = [row["claim"] for row in svc.query_rows("fstrim reclaim datastore space", limit=10)]
    ranked = rank_claim_rows(
        "fstrim reclaim datastore space",
        claims,
        mode="hybrid",
        limit=10,
        pending_supersession_ids=pending,
    )
    order = [row.claim.id for row in ranked]
    assert correction.id in order, "the correction must still be retrievable"
    assert order.index(correction.id) < order.index(false_claim.id), (
        "the superseded claim outranked its own correction"
    )

    # Same inputs without the pending set = the bug, reproduced.
    unfixed = rank_claim_rows(
        "fstrim reclaim datastore space", claims, mode="hybrid", limit=10
    )
    unfixed_order = [row.claim.id for row in unfixed]
    assert unfixed_order.index(false_claim.id) < unfixed_order.index(correction.id)


@pytest.mark.unit
def test_legacy_path_scores_the_penalty_even_though_it_does_not_reorder(tmp_path: Path) -> None:
    """`mode="legacy"` computes a score it never sorts by (pre-existing), so pin
    the score effect there rather than an order that path does not produce."""
    svc = _svc(tmp_path)
    old = _ingest(svc, "fstrim reclaim datastore space fully", confidence=0.97)
    _confirm(svc, old.id)
    _confirm(svc, _ingest(svc, "fstrim reclaim datastore space is impossible",
                          confidence=0.95, supersedes_claim_id=old.id).id)

    scored = {r["claim"].id: r["score"] for r in svc.query_rows("fstrim reclaim datastore space", limit=10)}
    assert scored[old.id] < 0.97, "the proposed-superseded claim kept full score"


@pytest.mark.unit
def test_superseded_claim_is_demoted_not_deleted(tmp_path: Path) -> None:
    """A demotion, never a disappearance: the outdated claim stays retrievable
    so provenance and audit trails survive."""
    svc = _svc(tmp_path)
    old = _ingest(svc, "the original measurement of datastore usage")
    _confirm(svc, old.id)
    _confirm(svc, _ingest(svc, "the corrected measurement of datastore usage",
                          supersedes_claim_id=old.id).id)

    found = [r["claim"].id for r in svc.query_rows("measurement of datastore usage", limit=10)]
    assert old.id in found


# ---------------------------------------------------------------------------
# False-positive direction -- a guard that fires on healthy claims is worse
# than no guard at all (pane-2, 2026-08-17)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("pending", [None, frozenset(), frozenset({99})])
def test_healthy_claims_are_never_penalised(pending) -> None:
    """Nothing outside the pending set may be demoted, ever."""
    assert _pending_supersession_penalty(_FakeClaim(42), pending) == 0.0


@pytest.mark.unit
def test_penalty_outweighs_the_core_tier_bonus() -> None:
    """Undersized, the penalty is decorative: a `core` outdated claim would keep
    beating a `working` correction on tier bonus alone."""
    penalty = _pending_supersession_penalty(_FakeClaim(42), frozenset({42}))
    assert penalty < 0.0
    assert abs(penalty) > _TIER_BONUS["core"]


@pytest.mark.unit
def test_ranking_is_unchanged_when_nothing_is_pending(tmp_path: Path) -> None:
    """Default path must rank byte-identically to before this feature."""
    svc = _svc(tmp_path)
    for text in ("alpha datastore fact", "beta datastore fact", "gamma datastore fact"):
        _confirm(svc, _ingest(svc, text).id)
    claims = [row["claim"] for row in svc.query_rows("datastore fact", limit=10)]

    base = rank_claim_rows("datastore fact", claims, limit=10)
    empty = rank_claim_rows(
        "datastore fact", claims, limit=10, pending_supersession_ids=frozenset()
    )
    assert [r.claim.id for r in base] == [r.claim.id for r in empty]
    assert [r.score for r in base] == [r.score for r in empty]


@pytest.mark.unit
def test_pending_lookup_survives_a_broken_event_log(tmp_path: Path) -> None:
    """A bookkeeping fault must degrade to "no penalty", never break recall."""
    svc = _svc(tmp_path)
    _confirm(svc, _ingest(svc, "a claim that must stay retrievable").id)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("event log unavailable")

    svc.list_events = _boom  # type: ignore[method-assign]
    svc._pending_supersession_cache = None

    assert pending_supersession_ids(svc) == frozenset()
    assert svc.query_rows("claim that must stay retrievable", limit=5)
