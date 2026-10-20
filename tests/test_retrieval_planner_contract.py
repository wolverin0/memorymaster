from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from memorymaster.recall.planner import RetrievalRequest, build_retrieval_plan
from memorymaster.core.service import MemoryService


@pytest.mark.parametrize("exact_ids", [[7], []])
def test_long_query_prefers_exact_matches_and_fills_from_or(exact_ids) -> None:
    query = "wezterm cli timeout node bash"
    service = MemoryService.__new__(MemoryService)

    def rows(ids):
        return [{"claim": SimpleNamespace(id=claim_id)} for claim_id in ids]

    service.query_rows = Mock(side_effect=lambda **kw: rows(
        [1, 7, 2] if " OR " in kw["query_text"] else exact_ids
    ))

    result = service.retrieve(RetrievalRequest(query_text=query, limit=3))

    assert [row["claim"].id for row in result.rows] == (
        [7, 1, 2] if exact_ids else [1, 7, 2]
    )
    assert service.query_rows.call_args_list[0].kwargs["query_text"] == query
    assert service.query_rows.call_count == 2


@pytest.mark.parametrize("query, limit", [
    ("node bash", 1), ("node bash", 3), ("wezterm cli timeout node bash", 1),
])
def test_short_queries_and_full_exact_results_do_not_expand(query, limit) -> None:
    service = MemoryService.__new__(MemoryService)
    service.query_rows = Mock(return_value=[{"claim": SimpleNamespace(id=7)}])

    result = service.retrieve(RetrievalRequest(query_text=query, limit=limit))

    assert len(result.rows) == 1
    service.query_rows.assert_called_once()
    assert service.query_rows.call_args.kwargs["query_text"] == query


@pytest.mark.parametrize("trust_mode", ["trusted", "exploratory"])
def test_fallback_preserves_all_retrieval_options(trust_mode) -> None:
    service = MemoryService.__new__(MemoryService)
    service.query_rows = Mock(return_value=[])
    service.retrieve(RetrievalRequest(
        query_text="wezterm cli timeout node bash", limit=4,
        trust_mode=trust_mode, allow_sensitive=True,
        scope_allowlist=("project:fixture",), requesting_agent="fixture-agent",
    ))
    first, second = [call.kwargs.copy() for call in service.query_rows.call_args_list]
    first.pop("query_text")
    second.pop("query_text")
    assert first == second
    assert first["limit"] == 4
    assert first["include_stale"] is (trust_mode == "exploratory")
    assert first["include_conflicted"] is (trust_mode == "exploratory")
    assert first["include_candidates"] is (trust_mode == "exploratory")
    assert first["allow_sensitive"] is True
    assert first["scope_allowlist"] == ["project:fixture"]
    assert first["requesting_agent"] == "fixture-agent"


@pytest.mark.parametrize("surface", ["service", "hook", "review"])
def test_sqlite_surfaces_prioritize_all_terms_and_recover_or_candidates(
    tmp_path, monkeypatch, surface,
) -> None:
    from memorymaster.core.lifecycle import transition_claim
    from memorymaster.core.models import CitationInput
    from memorymaster.operations.operational_review import _default_retrieval
    from memorymaster.recall.context_hook import _governed_prompt_rows

    monkeypatch.setenv("MEMORYMASTER_SCOPE", "project:fixture")
    db = tmp_path / "retrieval.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    claims = []
    for text, status in [
        ("wezterm cli timeout node bash", "confirmed"),
        ("wezterm cli fallback behavior", "confirmed"),
        ("wezterm cli timeout node bash candidate", "candidate"),
    ]:
        claim = service.ingest(
            text, [CitationInput(source="test://and-first")], scope="global",
        )
        if status == "confirmed":
            transition_claim(service.store, claim.id, status,
                             reason="retrieval fixture", event_type="validator")
        claims.append(claim)

    def retrieve(query):
        if surface == "service":
            return [claim.human_id for claim in service.query(query, limit=5)]
        if surface == "hook":
            return [row["claim"].human_id for row in
                    _governed_prompt_rows(service, query, limit=5)]
        return _default_retrieval(db, query)

    exact, partial, candidate = [claim.human_id for claim in claims]
    assert retrieve("wezterm cli timeout node bash") == [exact, partial]
    fallback = retrieve("wezterm cli timeout node absentword")
    assert set(fallback) == {exact, partial}
    assert candidate not in fallback
    short_rows = service.query_rows(
        "wezterm cli", limit=5, include_stale=False, include_conflicted=False,
        record_accesses=False,
    )
    assert retrieve("wezterm cli") == [row["claim"].human_id for row in short_rows]


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
