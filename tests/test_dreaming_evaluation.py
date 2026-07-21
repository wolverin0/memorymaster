from __future__ import annotations

import json

from memorymaster.dreaming.evaluation import evaluate_records, load_jsonl


def test_evaluation_scores_usefulness_and_refuses_small_sample_activation(tmp_path) -> None:
    records = [
        {
            "record_id": "stable-1",
            "should_emit": True,
            "emitted": True,
            "evidence_exact": True,
            "expected_scope": "personal",
            "actual_scope": "personal",
            "expected_action": "add",
            "actual_action": "add",
            "structured_valid": True,
            "human_accept": True,
        },
        {
            "record_id": "ephemeral-1",
            "should_emit": False,
            "emitted": False,
            "structured_valid": True,
        },
    ]

    report = evaluate_records(records)

    assert report["metrics"]["evidence_precision"] == 1.0
    assert report["metrics"]["ephemeral_rejection"] == 1.0
    assert report["metrics"]["scope_isolation"] == 1.0
    assert report["metrics"]["action_accuracy"] == 1.0
    assert report["activation_ready"] is False
    assert "minimum_labeled_decisions" in report["failed_gates"]

    path = tmp_path / "labels.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
    assert load_jsonl(path) == records


def test_evaluation_requires_complete_explicit_boolean_labels() -> None:
    report = evaluate_records([
        {"record_id": "bad", "should_emit": "yes", "emitted": True, "structured_valid": True}
    ])

    assert report["invalid_records"] == ["bad"]
    assert report["activation_ready"] is False
