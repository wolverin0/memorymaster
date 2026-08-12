from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from memorymaster.evaluation import paper_research as evaluator


FIXTURE = Path(__file__).parent / "fixtures" / "paper_research_eval_v1.jsonl"


def _perfect_prediction(case: dict, profile: str) -> dict:
    expected = case["expected"]
    tool = expected["tool"]
    predicted_tool = None
    if tool is not None:
        predicted_tool = {
            "name": tool["name"],
            "arguments": {
                name: spec["value"]
                for name, spec in tool["parameters"].items()
                if spec["source"] != "missing"
            },
            "parameter_sources": {
                name: spec["source"] for name, spec in tool["parameters"].items()
            },
        }
    return {
        "schema_version": evaluator.PREDICTION_SCHEMA,
        "case_id": case["id"],
        "profile": profile,
        "answer": " / ".join(expected["answer_contains"]),
        "citations": list(expected["citations"]),
        "retrieved_ids": list(expected["retrieved_ids"]),
        "used_ids": list(expected["used_ids"]),
        "preserved_values": list(expected["lossless_values"]),
        "tool": predicted_tool,
    }


def _perfect_matrix(cases: list[dict]) -> list[dict]:
    return [
        _perfect_prediction(case, profile)
        for case in cases
        for profile in evaluator.PROFILES
    ]


def test_publishable_fixture_is_versioned_synthetic_and_covers_required_categories() -> None:
    cases = evaluator.load_cases(FIXTURE)

    assert len(cases) == len(evaluator.REQUIRED_CATEGORIES)
    assert {case["category"] for case in cases} == set(evaluator.REQUIRED_CATEGORIES)
    assert all(case["synthetic"] is True for case in cases)


def test_case_validation_fails_closed_on_schema_synthetic_and_duplicate_errors(
    tmp_path: Path,
) -> None:
    case = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[0])
    invalid = {**case, "schema_version": "unknown", "synthetic": False}
    path = tmp_path / "invalid.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in (invalid, invalid)), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        evaluator.load_cases(path, require_all_categories=False)


def test_perfect_five_profile_matrix_scores_each_dimension_independently() -> None:
    cases = evaluator.load_cases(FIXTURE)

    report = evaluator.evaluate(cases, _perfect_matrix(cases))

    assert report["schema_version"] == evaluator.REPORT_SCHEMA
    assert set(report["profiles"]) == set(evaluator.PROFILES)
    assert report["gate_pass"] is True
    for metrics in report["profiles"].values():
        assert metrics["answer_accuracy"] == 1.0
        assert metrics["citation_accuracy"] == 1.0
        assert metrics["parameter_accuracy"] == 1.0
        assert metrics["parameter_source_accuracy"] == 1.0
        assert metrics["failures"] == {"none": len(cases)}


@pytest.mark.parametrize(
    ("case_id", "mutation", "failure"),
    [
        ("latest-superseded-001", {"retrieved_ids": []}, "retrieval_miss"),
        ("latest-superseded-001", {"preserved_values": []}, "lossless_retention_failure"),
        ("latest-superseded-001", {"used_ids": []}, "retrieved_but_unused"),
        ("tool-parameters-001", {"tool.name": "calendar.delete_event"}, "wrong_tool"),
        ("parameter-provenance-001", {"tool.default": ["urgency", "high"]}, "hallucinated_default"),
        ("tool-parameters-001", {"tool.argument": ["duration_minutes", 60]}, "tool_argument_error"),
        ("latest-superseded-001", {"answer": "unknown"}, "answer_error"),
        ("latest-superseded-001", {"citations": []}, "citation_error"),
    ],
)
def test_failure_attribution_is_specific(
    case_id: str,
    mutation: dict,
    failure: str,
) -> None:
    cases = evaluator.load_cases(FIXTURE)
    case = next(row for row in cases if row["id"] == case_id)
    prediction = _perfect_prediction(case, evaluator.PROFILES[0])
    prediction = deepcopy(prediction)
    if "tool.name" in mutation:
        prediction["tool"]["name"] = mutation["tool.name"]
    elif "tool.default" in mutation:
        name, value = mutation["tool.default"]
        prediction["tool"]["arguments"][name] = value
        prediction["tool"]["parameter_sources"][name] = "default"
    elif "tool.argument" in mutation:
        name, value = mutation["tool.argument"]
        prediction["tool"]["arguments"][name] = value
    else:
        prediction.update(mutation)

    score = evaluator.score_case(case, prediction)

    assert score["failure_mode"] == failure


def test_parameter_values_and_provenance_are_separate_scores() -> None:
    cases = evaluator.load_cases(FIXTURE)
    case = next(row for row in cases if row["id"] == "tool-parameters-001")
    prediction = _perfect_prediction(case, evaluator.PROFILES[0])
    prediction["tool"]["parameter_sources"]["timezone"] = "default"

    score = evaluator.score_case(case, prediction)

    assert score["parameter_exact"] is True
    assert score["parameter_sources_correct"] is False
    assert score["failure_mode"] == "tool_argument_error"


def test_evaluate_rejects_an_incomplete_or_duplicate_matrix() -> None:
    cases = evaluator.load_cases(FIXTURE)
    predictions = _perfect_matrix(cases)

    with pytest.raises(ValueError, match="missing predictions"):
        evaluator.evaluate(cases, predictions[:-1])
    with pytest.raises(ValueError, match="duplicate prediction"):
        evaluator.evaluate(cases, [*predictions, predictions[0]])


def test_report_excludes_queries_answers_and_preserved_values() -> None:
    cases = evaluator.load_cases(FIXTURE)

    report_text = json.dumps(evaluator.evaluate(cases, _perfect_matrix(cases)))

    assert "absolutely delighted, not merely satisfied" not in report_text
    assert "Which studio currently handles" not in report_text
    assert "Northwind Studio" not in report_text


def test_cli_writes_deterministic_aggregate_report(tmp_path: Path) -> None:
    cases = evaluator.load_cases(FIXTURE)
    predictions = tmp_path / "predictions.jsonl"
    output = tmp_path / "report.json"
    predictions.write_text(
        "\n".join(json.dumps(row) for row in _perfect_matrix(cases)) + "\n",
        encoding="utf-8",
    )

    exit_code = evaluator.main(
        ["--cases", str(FIXTURE), "--predictions", str(predictions), "--output", str(output)]
    )

    assert exit_code == 0
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["gate_pass"] is True
    assert persisted["dataset_fingerprint"]
    assert persisted["prediction_fingerprint"]
