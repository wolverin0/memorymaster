from __future__ import annotations

import json

import pytest

from memorymaster.evaluation import budget_policy


def _row(row_id: str, text: str, **overrides) -> dict:
    row = {
        "id": row_id,
        "text": text,
        "scope": "project:synthetic",
        "sensitive": False,
        "status": "confirmed",
        "confidence": 0.9,
        "evidence_count": 1,
        "subject": "synthetic-subject",
        "predicate": "uses",
        "object_value": row_id,
    }
    row.update(overrides)
    return row


def test_versioned_policies_are_explicit_and_never_auto_inferred() -> None:
    assert set(budget_policy.POLICIES) == {"low", "balanced", "high", "temporal", "procedural"}
    assert budget_policy.get_policy("low").provider_calls_allowed == 0
    assert budget_policy.get_policy("procedural").include_skills is True
    with pytest.raises(ValueError, match="requested_tier"):
        budget_policy.get_policy("auto")


def test_scope_and_sensitivity_filter_before_policy_admission() -> None:
    rows = [
        _row("ok", "Synthetic safe memory"),
        _row("cross-scope", "PRIVATE CROSS SCOPE", scope="project:other"),
        _row("sensitive", "PRIVATE SENSITIVE", sensitive=True),
    ]

    report = budget_policy.shadow_admit(rows, requested_tier="low", scope_allowlist=["project:synthetic"])
    rendered = json.dumps(report)

    assert report["pipeline"][:3] == ["scope_filter", "sensitivity_filter", "policy_selection"]
    assert report["authorized_count"] == 1
    assert "cross-scope" not in rendered
    assert "sensitive" not in rendered
    assert "PRIVATE" not in rendered


def test_admission_diagnoses_duplicate_near_duplicate_and_weak_support() -> None:
    rows = [
        _row("first", "Alpha beta gamma delta"),
        _row("duplicate", "alpha beta gamma delta"),
        _row("near", "Alpha beta gamma delta epsilon"),
        _row("weak", "Distinct weak claim", evidence_count=0, confidence=0.4),
    ]

    report = budget_policy.shadow_admit(rows, requested_tier="balanced", scope_allowlist=["project:synthetic"])

    assert report["admitted_ids"] == ["first"]
    assert report["diagnostics"] == {
        "duplicate": ["redundant"],
        "near": ["near_duplicate"],
        "weak": ["weak_support"],
    }


def test_lifecycle_conflict_is_visible_without_silently_picking_truth() -> None:
    rows = [
        _row("blue", "Synthetic setting is blue", object_value="blue"),
        _row("green", "Synthetic setting is green", object_value="green"),
    ]

    report = budget_policy.shadow_admit(rows, requested_tier="high", scope_allowlist=["project:synthetic"])

    assert report["admitted_ids"] == ["blue", "green"]
    assert report["diagnostics"]["blue"] == ["lifecycle_conflict"]
    assert report["diagnostics"]["green"] == ["lifecycle_conflict"]


def test_shadow_policy_is_replay_deterministic_and_content_free() -> None:
    rows = [_row(f"row-{index}", f"Unique synthetic memory {index}") for index in range(12)]

    first = budget_policy.shadow_admit(rows, requested_tier="low", scope_allowlist=["project:synthetic"])
    second = budget_policy.shadow_admit(rows, requested_tier="low", scope_allowlist=["project:synthetic"])

    assert first == second
    assert len(first["admitted_ids"]) == budget_policy.get_policy("low").candidate_limit
    assert "Unique synthetic memory" not in json.dumps(first)
    assert first["provider_calls"] == 0


def test_admission_stage_observation_carries_selected_tier_and_zero_provider_calls() -> None:
    report = budget_policy.shadow_admit(
        [_row("one", "Synthetic one")],
        requested_tier="temporal",
        scope_allowlist=["project:synthetic"],
    )

    observation = budget_policy.admission_observation(report, elapsed_ms=3.5)

    assert observation.stage == "admission"
    assert observation.selected_tier == "temporal"
    assert observation.provider_calls == 0
    assert observation.content_chars_read == 0
