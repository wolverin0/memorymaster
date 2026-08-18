"""The supersession demotion must fire on the DEFAULT retrieval path.

WHY THIS EXISTS: the first version of this fix (c832df4) folded the penalty into
`score` and proved it with a test pinned to `mode="hybrid"`. But `score` is the
one thing the legacy branch of ``rank_claim_rows`` never sorts by -- it returns
the store's bm25 order untouched. And legacy is the DEFAULT everywhere that
matters: ``query_memory`` defaults to it, and the recall hook hardcodes it
(``context_hook.py`` retrieval_mode="legacy"); ``_query_legacy_mode`` only
escapes to hybrid when the query text contains `" OR "`, which ordinary
questions never do.

So the demotion was computed and discarded on exactly the path agents read
memory through -- the same "looks applied, does nothing" failure the penalty was
written to fix. These tests pin the behaviour at the default path, where the
previous test could not see it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall.retrieval import rank_claim_rows


def _svc(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "legacy-order.db", workspace_root=tmp_path)
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


@pytest.mark.unit
def test_default_path_demotes_a_pending_superseded_claim(tmp_path: Path) -> None:
    """The end-to-end case, through the path agents actually use.

    No `" OR "` in the query, so this stays on the legacy branch -- exactly
    where the demotion used to be inert.
    """
    svc = _svc(tmp_path)
    outdated = _ingest(svc, "fstrim reclaim datastore space fully", confidence=0.97)
    _confirm(svc, outdated.id)
    correction = _ingest(
        svc,
        "fstrim reclaim datastore space is impossible without the controller primitive",
        confidence=0.95,
        supersedes_claim_id=outdated.id,
    )
    _confirm(svc, correction.id)

    order = [row["claim"].id for row in svc.query_rows("fstrim reclaim datastore space", limit=10)]

    assert correction.id in order, "the correction must still be retrievable"
    assert outdated.id in order, "demoted, not hidden -- provenance must survive"
    assert order.index(correction.id) < order.index(outdated.id), (
        "the outdated claim still outranks its correction on the default path"
    )


@pytest.mark.unit
def test_legacy_keeps_store_order_for_healthy_claims(tmp_path: Path) -> None:
    """FALSE-POSITIVE DIRECTION: with nothing pending, legacy must return the
    store's ordering byte-identically. Re-ranking healthy results would be a
    far worse regression than the bug being fixed."""
    svc = _svc(tmp_path)
    for text in ("alpha datastore fact", "beta datastore fact", "gamma datastore fact"):
        _confirm(svc, _ingest(svc, text).id)
    claims = [row["claim"] for row in svc.query_rows("datastore fact", limit=10)]

    base = rank_claim_rows("datastore fact", claims, mode="legacy", limit=10)
    empty = rank_claim_rows(
        "datastore fact", claims, mode="legacy", limit=10, pending_supersession_ids=frozenset()
    )
    none_given = rank_claim_rows(
        "datastore fact", claims, mode="legacy", limit=10, pending_supersession_ids=None
    )

    assert [r.claim.id for r in base] == [c.id for c in claims], "store order must be preserved"
    assert [r.claim.id for r in empty] == [r.claim.id for r in base]
    assert [r.claim.id for r in none_given] == [r.claim.id for r in base]


@pytest.mark.unit
def test_only_the_flagged_claim_moves(tmp_path: Path) -> None:
    """Demoting must be surgical: every other claim keeps its relative order."""
    svc = _svc(tmp_path)
    ids = []
    for text in ("aaa shared topic", "bbb shared topic", "ccc shared topic", "ddd shared topic"):
        c = _ingest(svc, text)
        _confirm(svc, c.id)
        ids.append(c.id)
    claims = [row["claim"] for row in svc.query_rows("shared topic", limit=10)]
    baseline = [r.claim.id for r in rank_claim_rows("shared topic", claims, mode="legacy", limit=10)]

    flagged = baseline[1]  # demote something from the middle
    got = [
        r.claim.id
        for r in rank_claim_rows(
            "shared topic", claims, mode="legacy", limit=10,
            pending_supersession_ids=frozenset({flagged}),
        )
    ]

    assert got[-1] == flagged, "the flagged claim must land last"
    assert got[:-1] == [i for i in baseline if i != flagged], (
        "the untouched claims must keep their relative order"
    )
