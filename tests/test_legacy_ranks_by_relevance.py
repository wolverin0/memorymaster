"""Legacy mode must not rank an irrelevant claim above a relevant one.

The legacy branch of ``rank_claim_rows`` computes each claim's lexical score,
stores it for display, and then scores purely on confidence and bonuses --
``score = confidence + pinned_bonus + tier_bonus``. It also never sorts. So the
number shown as ``score`` encodes how *trusted and recent* a claim is, not how
well it answers the question, and the order is whatever the store handed over.

That stayed harmless while the store only ever returned strict AND matches:
every candidate was relevant, so ignoring relevance cost nothing. It became
visible as soon as the candidate set grew wider than the exact match, which is
what the zero-hit relaxation does.

Companion to ``test_legacy_path_supersession_order.py``, which documents the
same "legacy never sorts" property from the supersession side.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall.retrieval import rank_claim_rows


def _svc(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "legacy-rank.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _ingest(svc: MemoryService, text: str, confidence: float) -> None:
    svc.ingest(
        text=text,
        citations=[CitationInput(source="test", locator="l", excerpt="e")],
        scope="project:test",
        source_agent="pytest",
        confidence=confidence,
    )


def _candidates(svc: MemoryService):
    return svc.store.list_claims(limit=50, status_in=["candidate"])


QUERY = "what order should a startup sequence follow"


@pytest.fixture()
def mixed(tmp_path: Path) -> MemoryService:
    """A high-confidence irrelevant claim ingested before the relevant one."""
    svc = _svc(tmp_path)
    _ingest(svc, "The sensitivity filter is a mandatory firewall at ingest time.", 0.99)
    _ingest(svc, "Startup sequence must follow a strict three-step order.", 0.30)
    return svc


def test_relevant_claim_outranks_irrelevant_high_confidence_one(mixed):
    ranked = rank_claim_rows(QUERY, _candidates(mixed), mode="legacy", limit=5)
    assert "Startup sequence" in ranked[0].claim.text, (
        "a claim with zero lexical overlap outranked the one that actually "
        "answers the query, because legacy scores on confidence alone"
    )


def test_legacy_results_are_ordered_by_relevance(mixed):
    """The contract: relevance decides the order; ties keep the store's order."""
    ranked = rank_claim_rows(QUERY, _candidates(mixed), mode="legacy", limit=5)
    lex = [row.lexical_score for row in ranked]
    assert lex == sorted(lex, reverse=True), f"legacy returned unsorted rows: {lex}"


def test_confidence_still_breaks_ties_among_equally_relevant_claims(tmp_path):
    """Relevance leads, but the confidence signal must not be discarded."""
    svc = _svc(tmp_path)
    _ingest(svc, "Startup sequence must follow a strict order. Variant low.", 0.20)
    _ingest(svc, "Startup sequence must follow a strict order. Variant high.", 0.90)
    ranked = rank_claim_rows(
        "startup sequence order", _candidates(svc), mode="legacy", limit=5
    )
    assert "high" in ranked[0].claim.text


def test_order_is_preserved_when_nothing_is_relevant(tmp_path):
    """With no lexical signal at all, the prior confidence order stands."""
    svc = _svc(tmp_path)
    _ingest(svc, "Completely unrelated claim about turtles.", 0.90)
    _ingest(svc, "Another unrelated statement regarding turtles.", 0.20)
    ranked = rank_claim_rows("xyzzy plugh", _candidates(svc), mode="legacy", limit=5)
    assert [round(row.score, 3) for row in ranked] == sorted(
        [round(row.score, 3) for row in ranked], reverse=True
    )
