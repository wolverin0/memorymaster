from __future__ import annotations

import json
import pytest

from memorymaster.dreaming.evaluation import evaluate_records, load_jsonl


def labeled(record_id, should_emit=True, emitted=True, **overrides):
    return {
        "record_id": record_id, "should_emit": should_emit, "emitted": emitted,
        "structured_valid": True, "evidence_exact": True,
        "expected_scope": "project:test", "actual_scope": "project:test",
        "expected_action": "add", "actual_action": "add",
        "human_accept": True, "label_origin": "human", **overrides,
    }


def test_missed_useful_facts_cannot_pass_activation():
    records = [labeled(f"good-{i}") for i in range(20)]
    records += [labeled(f"miss-{i}", emitted=False) for i in range(30)]
    records += [labeled(f"routine-{i}", should_emit=False, emitted=False) for i in range(20)]
    report = evaluate_records(records)
    assert report["metrics"]["useful_recall"] == .4
    assert report["counts"]["missed_useful"] == 30
    assert "useful_recall" in report["failed_gates"]
    assert not report["activation_ready"]


def test_balanced_labeled_population_can_pass():
    records = [labeled(f"good-{i}") for i in range(25)]
    records += [labeled(f"routine-{i}", should_emit=False, emitted=False) for i in range(25)]
    report = evaluate_records(records)
    assert report["activation_ready"]
    assert report["metrics"]["useful_precision"] == 1
    assert report["metrics"]["useful_recall"] == 1


@pytest.mark.parametrize("origin", ["ai", "synthetic", None])
def test_nonhuman_labels_do_not_become_human_reviews(origin):
    records = [labeled(f"good-{i}", label_origin=origin) for i in range(25)]
    records += [labeled(f"routine-{i}", should_emit=False, emitted=False) for i in range(25)]
    report = evaluate_records(records)
    assert report["human_reviews"] == 0
    assert not report["activation_ready"]


def test_duplicate_ids_cannot_pad_sample_size():
    report = evaluate_records([labeled("same")] * 60)
    assert not report["activation_ready"]
    assert "duplicate_record_ids" in report["failed_gates"]


@pytest.mark.parametrize("record", [None, [], {"record_id": None},
                                   labeled("", emitted=False)])
def test_invalid_record_shapes_fail_closed(record):
    report = evaluate_records([record])
    assert report["invalid_records"]
    assert not report["activation_ready"]


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
