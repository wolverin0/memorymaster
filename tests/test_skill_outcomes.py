from __future__ import annotations

import json

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.evaluation.skill_outcomes import (
    SkillOutcomeValidationError,
    evaluate_skill_outcomes,
    write_skill_outcome_report,
)
from memorymaster.knowledge.skill_schema import build_skill_fields
from memorymaster.knowledge.skills import approve_skill_candidate


SCOPE = "project:memorymaster"
SCHEMA_HASH = "a" * 64


def _payload(slug="verify-release"):
    return {
        "schema": "personal-skill-v1",
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "when_to_use": "Before a release candidate is accepted.",
        "when_not_to_use": "Outside release work.",
        "inputs": ["candidate commit"],
        "prerequisites": ["disposable database"],
        "workflow": ["Run the release gate."],
        "decision_rules": ["Stop on a failed invariant."],
        "expected_output": "A bounded verification report.",
        "validation": ["Confirm every required check passes."],
        "pitfalls": ["Partial checks are not release proof."],
        "recovery": ["Keep the release unpublished."],
        "quality_scores": {
            "recurrence": 16,
            "reusability": 16,
            "executability": 16,
            "validation": 16,
            "safety": 16,
        },
    }


def _service(tmp_path):
    service = MemoryService(tmp_path / "outcomes.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _skill(service, *, scope=SCOPE, status="confirmed", slug="verify-release"):
    claim = service.ingest(
        **build_skill_fields(_payload(slug), supporting_claim_ids=[1]),
        citations=[CitationInput(source="fixture", locator="skill:verify-release")],
        scope=scope,
        source_agent="skill-reviewer",
    )
    if status == "confirmed":
        approve_skill_candidate(service, claim.id, actor="fixture-operator")
        claim = service.store.get_claim(claim.id)
    return claim


def _observation(skill_id, **overrides):
    observation = {
        "execution_ref": "fixture-execution-1",
        "skill_claim_id": skill_id,
        "skill_version": 1,
        "outcome": "success",
        "observed_at": "2026-08-08T12:00:00Z",
        "consumer_profile": "codex",
        "model_profile": "gpt-5.4-mini",
        "tool_name": "pytest",
        "tool_schema_sha256": SCHEMA_HASH,
        "activation_matched": True,
        "termination_result": "passed",
        "validation_result": "passed",
        "metrics": {"elapsed_ms": 1200, "attempts": 1, "tool_calls": 1},
    }
    observation.update(overrides)
    return observation


def _lifecycle(service, claim_id):
    claim = service.store.get_claim(claim_id)
    return (claim.status, claim.version, claim.confidence, claim.object_value, claim.updated_at)


def test_success_emits_review_signal_without_mutating_skill(tmp_path):
    service = _service(tmp_path)
    skill = _skill(service)
    before = _lifecycle(service, skill.id)

    report = evaluate_skill_outcomes(
        service, [_observation(skill.id)], scope_allowlist=[SCOPE]
    )

    assert report["counts"] == {
        "success": 1,
        "failure": 0,
        "ambiguous": 0,
        "positive_review": 1,
        "warnings": 0,
    }
    assert report["observations"][0]["review_signal"] == "positive_review"
    assert report["warnings"] == []
    assert _lifecycle(service, skill.id) == before


def test_failure_creates_separate_warning_and_never_positive_reinforcement(tmp_path):
    service = _service(tmp_path)
    skill = _skill(service)

    report = evaluate_skill_outcomes(
        service,
        [
            _observation(
                skill.id,
                outcome="failure",
                termination_result="failed",
                validation_result="failed",
            )
        ],
        scope_allowlist=[SCOPE],
    )

    assert report["counts"]["positive_review"] == 0
    assert report["observations"][0]["review_signal"] == "negative_warning"
    assert report["warnings"][0]["code"] == "skill_execution_failed"


def test_ambiguous_outcome_is_neutral_and_bounded(tmp_path):
    service = _service(tmp_path)
    skill = _skill(service)

    report = evaluate_skill_outcomes(
        service,
        [
            _observation(
                skill.id,
                outcome="ambiguous",
                termination_result="not_checked",
                validation_result="not_checked",
                metrics={},
            )
        ],
        scope_allowlist=[SCOPE],
    )

    assert report["observations"][0]["review_signal"] == "neutral_review"
    assert report["warnings"][0]["code"] == "skill_execution_ambiguous"


def test_identical_replay_is_deduplicated_and_deterministic(tmp_path):
    service = _service(tmp_path)
    skill = _skill(service)
    observation = _observation(skill.id)

    first = evaluate_skill_outcomes(
        service, [observation, dict(observation)], scope_allowlist=[SCOPE]
    )
    second = evaluate_skill_outcomes(
        service, [observation, dict(observation)], scope_allowlist=[SCOPE]
    )

    assert first == second
    assert len(first["observations"]) == 1
    assert first["diagnostics"]["duplicates"] == 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"outcome": "maybe"}, "outcome"),
        ({"tool_payload": {"raw": True}}, "unknown"),
        ({"tool_schema_sha256": "short"}, "sha256"),
        ({"consumer_profile": "sk-test-abcdefghijklmnopqrstuvwxyz"}, "sensitive"),
        ({"metrics": {"elapsed_ms": -1}}, "elapsed_ms"),
    ],
)
def test_malformed_or_raw_observations_fail_closed(tmp_path, change, message):
    service = _service(tmp_path)
    skill = _skill(service)

    with pytest.raises(SkillOutcomeValidationError, match=message):
        evaluate_skill_outcomes(
            service, [_observation(skill.id, **change)], scope_allowlist=[SCOPE]
        )


def test_candidate_cross_scope_and_version_mismatch_are_rejected(tmp_path):
    service = _service(tmp_path)
    candidate = _skill(service, status="candidate", slug="candidate-skill")
    cross = _skill(service, scope="project:other", slug="cross-scope-skill")
    confirmed = _skill(service, slug="versioned-skill")

    report = evaluate_skill_outcomes(
        service,
        [
            _observation(candidate.id, execution_ref="candidate"),
            _observation(cross.id, execution_ref="cross"),
            _observation(confirmed.id, execution_ref="version", skill_version=2),
        ],
        scope_allowlist=[SCOPE],
    )

    assert report["observations"] == []
    assert report["diagnostics"]["unauthorized_skill"] == 2
    assert report["diagnostics"]["version_mismatch"] == 1


def test_content_free_report_artifact_contains_no_execution_reference(tmp_path):
    service = _service(tmp_path)
    skill = _skill(service)
    report = evaluate_skill_outcomes(
        service, [_observation(skill.id)], scope_allowlist=[SCOPE]
    )
    target = tmp_path / "artifacts" / "skill-outcomes.json"

    write_skill_outcome_report(report, target)
    stored = target.read_text(encoding="utf-8")

    assert json.loads(stored) == report
    assert "fixture-execution-1" not in stored
    assert "tool_payload" not in stored
