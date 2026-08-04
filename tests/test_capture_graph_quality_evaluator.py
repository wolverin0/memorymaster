from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import evaluate_capture_graph_quality as evaluator  # noqa: E402


def _case(case_id: str = "case-1") -> dict:
    return {
        "id": case_id,
        "synthetic": True,
        "quality_gate": True,
        "input": {"kind": "text", "text": "Alice uses Atlas."},
        "expected": {
            "claims": [
                {
                    "type": "fact",
                    "subject": "Alice",
                    "predicate": "uses",
                    "object": "Atlas",
                }
            ],
            "entities": ["Alice", "Atlas"],
            "relations": [["Alice", "uses", "Atlas"]],
        },
    }


def test_metric_requires_precision_and_statistical_lower_bound() -> None:
    passing = evaluator._metric(evaluator.Counts(40, 40, 40), 0.90)
    small = evaluator._metric(evaluator.Counts(3, 3, 3), 0.90)

    assert passing["pass"] is True
    assert passing["wilson_95_lower"] > 0.90
    assert small["precision"] == 1.0
    assert small["pass"] is False


def test_claim_and_relation_keys_are_deterministic() -> None:
    expected = _case()["expected"]
    claim = type(
        "Claim",
        (),
        {
            "claim_type": "FACT",
            "subject": " Alice ",
            "predicate": "uses",
            "object_value": "Atlas!",
        },
    )()
    relation = {"source": "ALICE", "relation": "uses", "target": "Atlas"}

    assert evaluator._counts(expected["claims"], [claim], evaluator._claim_key).correct == 1
    assert (
        evaluator._counts(
            expected["claims"], [claim], evaluator._typed_claim_key
        ).correct
        == 1
    )
    assert evaluator._counts(
        expected["relations"], [relation], evaluator._relation_key
    ).correct == 1


def test_fixture_rejects_small_or_non_synthetic_corpus(tmp_path: Path) -> None:
    fixture = tmp_path / "eval.jsonl"
    fixture.write_text(json.dumps(_case()) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="at least 2"):
        evaluator.load_fixture(fixture, min_cases=2)

    unsafe = _case()
    unsafe["synthetic"] = False
    fixture.write_text(json.dumps(unsafe) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="explicitly synthetic"):
        evaluator.load_fixture(fixture, min_cases=1)


def test_fixture_ignores_non_text_cases_for_quality_gate(tmp_path: Path) -> None:
    text_case = _case()
    url_case = _case("url-case")
    url_case["input"] = {"kind": "url", "source_uri": "https://example.test"}
    fixture = tmp_path / "eval.jsonl"
    fixture.write_text(
        "\n".join(json.dumps(row) for row in (text_case, url_case)) + "\n",
        encoding="utf-8",
    )

    assert [row["id"] for row in evaluator.load_fixture(fixture, min_cases=1)] == [
        "case-1"
    ]


def test_output_must_remain_outside_repository(tmp_path: Path) -> None:
    assert evaluator._private_output(tmp_path / "result.json").is_absolute()
    with pytest.raises(ValueError, match="outside the repository"):
        evaluator._private_output(evaluator.ROOT / "result.json")
