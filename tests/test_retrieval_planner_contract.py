from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from memorymaster.recall.planner import RetrievalRequest, build_retrieval_plan


def test_trusted_plan_is_immutable_and_normalizes_conversational_search() -> None:
    request = RetrievalRequest(
        query_text="How does MemoryMaster explain governed claims and citations?",
        retrieval_mode="legacy",
    )

    plan = build_retrieval_plan(request)

    assert plan.trust_mode == "trusted"
    assert plan.statuses == ("confirmed",)
    assert " OR " in plan.search_text
    assert "how" not in plan.search_text.lower().split()
    with pytest.raises(FrozenInstanceError):
        request.limit = 99  # type: ignore[misc]


def test_trusted_mode_rejects_exploratory_status_expansion() -> None:
    request = RetrievalRequest(
        query_text="candidate",
        trust_mode="trusted",
        include_candidates=True,
    )

    with pytest.raises(ValueError, match="exploratory"):
        build_retrieval_plan(request)


def test_exploratory_plan_carries_all_reviewable_statuses() -> None:
    plan = build_retrieval_plan(
        RetrievalRequest(query_text="candidate", trust_mode="exploratory")
    )

    assert plan.statuses == ("confirmed", "stale", "conflicted", "candidate")


def test_qdrant_request_remains_contained_until_governed_rehydration() -> None:
    plan = build_retrieval_plan(
        RetrievalRequest(query_text="semantic request", retrieval_mode="qdrant")
    )

    assert plan.requested_mode == "qdrant"
    assert plan.effective_mode == "legacy"
    assert plan.containment_reason
