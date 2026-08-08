"""Deterministic scoring for the synthetic paper-research evaluation matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence


CASE_SCHEMA = "memorymaster.paper-research-case.v1"
PREDICTION_SCHEMA = "memorymaster.paper-research-prediction.v1"
REPORT_SCHEMA = "memorymaster.paper-research-report.v1"
PROFILES = (
    "claims-only",
    "evidence-only",
    "claims+evidence",
    "claims+approved-skills",
    "claims+ephemeral-guidance",
)
REQUIRED_CATEGORIES = (
    "latest_superseded",
    "occurrence_dialogue_time",
    "valid_interval",
    "durative_state",
    "affect_emphasis",
    "narrative_arc",
    "tool_parameters",
    "parameter_provenance",
)
PARAMETER_SOURCES = {"explicit", "default", "inferred", "missing"}
_NORMALIZE = re.compile(r"[^\w]+", re.UNICODE)
_EXPECTED_LISTS = (
    "answer_contains",
    "citations",
    "retrieved_ids",
    "used_ids",
    "lossless_values",
)
_PREDICTION_LISTS = (
    "citations",
    "retrieved_ids",
    "used_ids",
    "preserved_values",
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {line_number}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"line {line_number} must contain a JSON object")
        rows.append(row)
    if not rows:
        raise ValueError("JSONL input must contain at least one object")
    return rows


def _require_string_list(row: dict[str, Any], field: str, *, label: str) -> None:
    value = row.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label}.{field} must be a list of non-empty strings")


def _validate_expected_tool(tool: Any, *, case_id: str) -> None:
    if tool is None:
        return
    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
        raise ValueError(f"case {case_id} expected.tool must name a tool")
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError(f"case {case_id} expected.tool.parameters must be an object")
    for name, spec in parameters.items():
        source = spec.get("source") if isinstance(spec, dict) else None
        if not isinstance(name, str) or source not in PARAMETER_SOURCES:
            raise ValueError(f"case {case_id} has invalid parameter provenance")
        if source != "missing" and "value" not in spec:
            raise ValueError(f"case {case_id} parameter {name} requires a value")
        if source == "missing" and "value" in spec:
            raise ValueError(f"case {case_id} missing parameter {name} cannot have a value")


def _validate_case(case: dict[str, Any]) -> None:
    if case.get("schema_version") != CASE_SCHEMA:
        raise ValueError(f"case schema_version must be {CASE_SCHEMA}")
    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case id must be a non-empty string")
    if case.get("synthetic") is not True:
        raise ValueError(f"case {case_id} must be explicitly synthetic")
    if case.get("category") not in REQUIRED_CATEGORIES:
        raise ValueError(f"case {case_id} has an unsupported category")
    if not isinstance(case.get("query"), str) or not case["query"]:
        raise ValueError(f"case {case_id} query must be non-empty")
    expected = case.get("expected")
    if not isinstance(expected, dict):
        raise ValueError(f"case {case_id} expected must be an object")
    for field in _EXPECTED_LISTS:
        _require_string_list(expected, field, label=f"case {case_id} expected")
    _validate_expected_tool(expected.get("tool"), case_id=case_id)


def load_cases(path: Path, *, require_all_categories: bool = True) -> list[dict[str, Any]]:
    cases = _read_jsonl(path)
    for case in cases:
        _validate_case(case)
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate case id")
    present = {case["category"] for case in cases}
    missing = set(REQUIRED_CATEGORIES) - present
    if require_all_categories and missing:
        raise ValueError(f"fixture is missing required categories: {sorted(missing)}")
    return cases


def _validate_prediction(prediction: dict[str, Any]) -> None:
    if prediction.get("schema_version") != PREDICTION_SCHEMA:
        raise ValueError(f"prediction schema_version must be {PREDICTION_SCHEMA}")
    if not isinstance(prediction.get("case_id"), str) or not prediction["case_id"]:
        raise ValueError("prediction case_id must be a non-empty string")
    if prediction.get("profile") not in PROFILES:
        raise ValueError(f"prediction profile must be one of {PROFILES}")
    if not isinstance(prediction.get("answer"), str):
        raise ValueError("prediction answer must be a string")
    for field in _PREDICTION_LISTS:
        _require_string_list(prediction, field, label="prediction")
    tool = prediction.get("tool")
    if tool is not None and (
        not isinstance(tool, dict)
        or not isinstance(tool.get("name"), str)
        or not isinstance(tool.get("arguments"), dict)
        or not isinstance(tool.get("parameter_sources"), dict)
    ):
        raise ValueError("prediction tool must include name, arguments, and parameter_sources")


def load_predictions(path: Path) -> list[dict[str, Any]]:
    predictions = _read_jsonl(path)
    for prediction in predictions:
        _validate_prediction(prediction)
    return predictions


def _normalized(value: Any) -> str:
    return _NORMALIZE.sub(" ", str(value).casefold()).strip()


def _contains_all(actual: str, expected: Iterable[str]) -> bool:
    normalized = _normalized(actual)
    return all(_normalized(value) in normalized for value in expected)


def _covers(actual: Iterable[str], required: Iterable[str]) -> bool:
    return set(required) <= set(actual)


def _tool_scores(expected: Any, actual: Any) -> dict[str, bool]:
    if expected is None:
        correct = actual is None
        return {"name": correct, "parameters": correct, "sources": correct, "hallucinated_default": False}
    if not isinstance(actual, dict):
        return {"name": False, "parameters": False, "sources": False, "hallucinated_default": False}
    expected_parameters = expected["parameters"]
    arguments = actual.get("arguments", {})
    sources = actual.get("parameter_sources", {})
    required_args = {name for name, spec in expected_parameters.items() if spec["source"] != "missing"}
    values_match = set(arguments) == required_args and all(
        arguments.get(name) == spec.get("value")
        for name, spec in expected_parameters.items()
        if spec["source"] != "missing"
    )
    expected_sources = {name: spec["source"] for name, spec in expected_parameters.items()}
    hallucinated = any(
        spec["source"] == "missing" and name in arguments and sources.get(name) == "default"
        for name, spec in expected_parameters.items()
    ) or any(name not in expected_parameters and source == "default" for name, source in sources.items())
    return {
        "name": actual.get("name") == expected["name"],
        "parameters": values_match,
        "sources": sources == expected_sources,
        "hallucinated_default": hallucinated,
    }


def _failure_mode(score: dict[str, bool]) -> str:
    ordered = (
        ("retrieval_complete", "retrieval_miss"),
        ("lossless_preserved", "lossless_retention_failure"),
        ("use_complete", "retrieved_but_unused"),
        ("tool_name_correct", "wrong_tool"),
    )
    for field, failure in ordered:
        if not score[field]:
            return failure
    if score["hallucinated_default"]:
        return "hallucinated_default"
    if not score["parameter_exact"] or not score["parameter_sources_correct"]:
        return "tool_argument_error"
    if not score["answer_correct"]:
        return "answer_error"
    if not score["citation_correct"]:
        return "citation_error"
    return "none"


def score_case(case: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    _validate_case(case)
    _validate_prediction(prediction)
    if prediction["case_id"] != case["id"]:
        raise ValueError("prediction case_id does not match case")
    expected = case["expected"]
    tool = _tool_scores(expected["tool"], prediction["tool"])
    score: dict[str, Any] = {
        "case_id": case["id"],
        "category": case["category"],
        "profile": prediction["profile"],
        "answer_correct": _contains_all(prediction["answer"], expected["answer_contains"]),
        "citation_correct": set(prediction["citations"]) == set(expected["citations"]),
        "retrieval_complete": _covers(prediction["retrieved_ids"], expected["retrieved_ids"]),
        "use_complete": _covers(prediction["used_ids"], expected["used_ids"]),
        "lossless_preserved": all(
            _contains_all(" / ".join(prediction["preserved_values"]), [value])
            for value in expected["lossless_values"]
        ),
        "tool_name_correct": tool["name"],
        "parameter_exact": tool["parameters"],
        "parameter_sources_correct": tool["sources"],
        "hallucinated_default": tool["hallucinated_default"],
    }
    score["tool_correct"] = tool["name"] and tool["parameters"] and tool["sources"]
    score["failure_mode"] = _failure_mode(score)
    return score


def _validate_matrix(cases: Sequence[dict[str, Any]], predictions: Sequence[dict[str, Any]]) -> None:
    expected_pairs = {(case["id"], profile) for case in cases for profile in PROFILES}
    seen: set[tuple[str, str]] = set()
    for prediction in predictions:
        _validate_prediction(prediction)
        pair = (prediction["case_id"], prediction["profile"])
        if pair in seen:
            raise ValueError(f"duplicate prediction for {pair[0]} / {pair[1]}")
        if pair not in expected_pairs:
            raise ValueError(f"prediction targets unknown case/profile: {pair}")
        seen.add(pair)
    missing = expected_pairs - seen
    if missing:
        raise ValueError(f"missing predictions for {len(missing)} case/profile pairs")


def _mean(scores: Sequence[dict[str, Any]], field: str) -> float:
    return sum(bool(score[field]) for score in scores) / len(scores) if scores else 1.0


def _profile_metrics(scores: Sequence[dict[str, Any]]) -> dict[str, Any]:
    tool_scores = [score for score in scores if score["category"] in {"tool_parameters", "parameter_provenance"}]
    return {
        "cases": len(scores),
        "answer_accuracy": _mean(scores, "answer_correct"),
        "citation_accuracy": _mean(scores, "citation_correct"),
        "retrieval_accuracy": _mean(scores, "retrieval_complete"),
        "use_accuracy": _mean(scores, "use_complete"),
        "lossless_accuracy": _mean(scores, "lossless_preserved"),
        "tool_accuracy": _mean(tool_scores, "tool_correct"),
        "parameter_accuracy": _mean(tool_scores, "parameter_exact"),
        "parameter_source_accuracy": _mean(tool_scores, "parameter_sources_correct"),
        "failures": dict(sorted(Counter(score["failure_mode"] for score in scores).items())),
    }


def _fingerprint(rows: Iterable[dict[str, Any]]) -> str:
    ordered = sorted(rows, key=lambda row: (str(row.get("id", row.get("case_id", ""))), str(row.get("profile", ""))))
    canonical = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate(cases: Sequence[dict[str, Any]], predictions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    for case in cases:
        _validate_case(case)
    _validate_matrix(cases, predictions)
    case_by_id = {case["id"]: case for case in cases}
    scores = [score_case(case_by_id[row["case_id"]], row) for row in predictions]
    scores.sort(key=lambda row: (row["profile"], row["case_id"]))
    profiles = {
        profile: _profile_metrics([score for score in scores if score["profile"] == profile])
        for profile in PROFILES
    }
    return {
        "schema_version": REPORT_SCHEMA,
        "case_schema_version": CASE_SCHEMA,
        "prediction_schema_version": PREDICTION_SCHEMA,
        "dataset_fingerprint": _fingerprint(cases),
        "prediction_fingerprint": _fingerprint(predictions),
        "case_count": len(cases),
        "profile_count": len(PROFILES),
        "profiles": profiles,
        "gate_pass": all(score["failure_mode"] == "none" for score in scores),
        "cases": scores,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)
    predictions = load_predictions(args.predictions)
    report = evaluate(cases, predictions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"gate_pass": report["gate_pass"], "case_count": report["case_count"]}, sort_keys=True))
    return 0 if report["gate_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
