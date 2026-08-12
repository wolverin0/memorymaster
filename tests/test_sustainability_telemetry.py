from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.evaluation import sustainability


def _observation(stage: str = "retrieval", **overrides):
    values = {
        "stage": stage,
        "elapsed_ms": 12.5,
        "content_chars_read": 400,
        "provider_calls": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_state": "not_applicable",
        "selected_tier": "legacy",
        "fallback_reason": "none",
    }
    values.update(overrides)
    return sustainability.StageObservation(**values)


def test_stage_observation_fails_closed_on_unknown_labels_and_negative_counts() -> None:
    with pytest.raises(ValueError, match="stage"):
        _observation(stage="private query text")
    with pytest.raises(ValueError, match="fallback_reason"):
        _observation(fallback_reason="the user asked about a private document")
    with pytest.raises(ValueError, match="non-negative"):
        _observation(provider_calls=-1)


def test_measure_stage_uses_injected_clock_and_result_sizer() -> None:
    ticks = iter((10.0, 10.125))

    result, observation = sustainability.measure_stage(
        "evidence_map_back",
        lambda: ["alpha", "beta"],
        clock=lambda: next(ticks),
        content_sizer=lambda rows: sum(len(row) for row in rows),
        selected_tier="balanced",
        cache_state="miss",
    )

    assert result == ["alpha", "beta"]
    assert observation.elapsed_ms == 125.0
    assert observation.content_chars_read == 9
    assert observation.selected_tier == "balanced"
    assert observation.cache_state == "miss"


def test_report_aggregates_cost_and_correctness_without_payload_text() -> None:
    observations = (
        _observation("retrieval", content_chars_read=120, elapsed_ms=2.0),
        _observation(
            "answer_generation",
            elapsed_ms=8.0,
            provider_calls=1,
            input_tokens=30,
            output_tokens=7,
            reasoning_tokens=4,
            selected_tier="high",
        ),
    )

    report = sustainability.build_report(
        observations,
        profile="claims+evidence",
        correctness={"answer_correct": True, "citation_correct": False, "task_correct": None},
    )
    rendered = json.dumps(report)

    assert report["schema_version"] == sustainability.REPORT_SCHEMA
    assert report["totals"] == {
        "elapsed_ms": 10.0,
        "content_chars_read": 520,
        "provider_calls": 1,
        "tool_calls": 0,
        "input_tokens": 30,
        "output_tokens": 7,
        "reasoning_tokens": 4,
    }
    assert report["correctness"]["answer_correct"] is True
    assert "query" not in rendered.casefold()
    assert "evidence text" not in rendered.casefold()


def test_report_is_bounded_and_rejects_free_text_profile() -> None:
    with pytest.raises(ValueError, match="at most"):
        sustainability.build_report(
            tuple(_observation() for _ in range(sustainability.MAX_OBSERVATIONS + 1)),
            profile="claims-only",
        )
    with pytest.raises(ValueError, match="profile"):
        sustainability.build_report((_observation(),), profile="user said a secret")


def test_authoritative_context_evaluation_observes_retrieval_and_packing() -> None:
    claims = [
        SimpleNamespace(
            id=1,
            text="Synthetic alpha memory.",
            subject=None,
            predicate=None,
            object_value=None,
            status="confirmed",
            pinned=False,
            confidence=1.0,
            scope="project:synthetic",
            volatility="low",
            created_at="2026-08-08T00:00:00+00:00",
            updated_at="2026-08-08T00:00:00+00:00",
            last_validated_at=None,
            valid_until=None,
            citations=[],
        )
    ]
    rows = tuple(
        {
            "claim": claim,
            "score": 1.0,
            "lexical_score": 1.0,
            "freshness_score": 1.0,
            "confidence_score": 1.0,
            "vector_score": 0.0,
            "breakdown": {},
        }
        for claim in claims
    )

    class FakeService:
        def __init__(self):
            self.request = None

        def retrieve(self, request):
            self.request = request
            return SimpleNamespace(rows=rows)

    ticks = iter((1.0, 1.01, 2.0, 2.02))
    service = FakeService()

    result, observations = sustainability.observe_context_query(
        service,
        "raw query must not enter telemetry",
        scope_allowlist=["project:synthetic"],
        token_budget=200,
        selected_tier="low",
        clock=lambda: next(ticks),
    )

    assert [row.stage for row in observations] == ["retrieval", "packing"]
    assert observations[0].elapsed_ms == pytest.approx(10.0)
    assert observations[1].elapsed_ms == pytest.approx(20.0)
    assert all(row.content_chars_read == len(claims[0].text) for row in observations)
    assert service.request.scope_allowlist == ("project:synthetic",)
    assert result.rows[0]["claim"].id == 1
    assert "raw query" not in json.dumps(sustainability.build_report(observations, profile="claims-only"))


def test_every_planned_stage_has_a_strict_enum_value() -> None:
    assert set(sustainability.STAGES) == {
        "retrieval",
        "graph_expansion",
        "evidence_map_back",
        "admission",
        "packing",
        "skill_recall",
        "skill_review",
        "answer_generation",
        "judge_generation",
    }


def test_disposable_sqlite_retrieval_emits_aggregate_safe_stage_artifact(tmp_path) -> None:
    service = MemoryService(tmp_path / "sustainability.db", workspace_root=tmp_path)
    service.init_db()
    claim = service.ingest(
        text="The synthetic orchid calibration uses setting cedar-seven.",
        citations=[CitationInput(source="synthetic", locator="evidence:orchid-1")],
        scope="project:synthetic",
    )
    service.store.apply_status_transition(
        claim,
        to_status="confirmed",
        reason="synthetic evaluation fixture",
        event_type="validator",
    )

    result, observations = sustainability.observe_context_query(
        service,
        "What setting does the synthetic orchid calibration use?",
        scope_allowlist=["project:synthetic"],
        retrieval_mode="legacy",
        selected_tier="low",
    )
    report = sustainability.build_report(observations, profile="claims-only")

    assert result.rows
    assert result.rows[0]["claim"].id == claim.id
    assert report["totals"]["provider_calls"] == 0
    assert report["totals"]["content_chars_read"] > 0
    assert "cedar-seven" not in json.dumps(report)
    assert "orchid calibration" not in json.dumps(report)
