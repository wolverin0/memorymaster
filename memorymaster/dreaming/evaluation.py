"""Offline usefulness and activation gates for native Dreaming."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


ACTIVATION_THRESHOLDS = {
    "minimum_labeled_decisions": 50,
    "minimum_human_reviews": 20,
    "evidence_precision": 0.95,
    "ephemeral_rejection": 0.90,
    "scope_isolation": 1.0,
    "action_accuracy": 0.85,
    "structured_yield": 0.95,
    "human_acceptance": 0.80,
}


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"line {line_number} must be a JSON object")
        records.append(value)
    return records


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _valid_record(record: dict[str, Any]) -> bool:
    required_booleans = ("should_emit", "emitted", "structured_valid")
    if any(type(record.get(key)) is not bool for key in required_booleans):
        return False
    if not record["emitted"]:
        return True
    if type(record.get("evidence_exact")) is not bool:
        return False
    return all(
        isinstance(record.get(key), str) and bool(record[key].strip())
        for key in ("expected_scope", "actual_scope", "expected_action", "actual_action")
    )


def evaluate_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(records)
    invalid = [str(row.get("record_id", index)) for index, row in enumerate(rows) if not _valid_record(row)]
    valid = [row for row in rows if _valid_record(row)]
    emitted = [row for row in valid if row["emitted"]]
    ephemeral = [row for row in valid if not row["should_emit"]]
    reviewed = [row for row in emitted if type(row.get("human_accept")) is bool]

    metrics = {
        "evidence_precision": _ratio(sum(bool(row["evidence_exact"]) for row in emitted), len(emitted)),
        "ephemeral_rejection": _ratio(sum(not row["emitted"] for row in ephemeral), len(ephemeral)),
        "scope_isolation": _ratio(
            sum(row["expected_scope"] == row["actual_scope"] for row in emitted), len(emitted)
        ),
        "action_accuracy": _ratio(
            sum(row["expected_action"] == row["actual_action"] for row in emitted), len(emitted)
        ),
        "structured_yield": _ratio(sum(row["structured_valid"] for row in valid), len(valid)),
        "human_acceptance": _ratio(sum(row["human_accept"] for row in reviewed), len(reviewed)),
    }
    failed: list[str] = []
    if len(valid) < ACTIVATION_THRESHOLDS["minimum_labeled_decisions"]:
        failed.append("minimum_labeled_decisions")
    if len(reviewed) < ACTIVATION_THRESHOLDS["minimum_human_reviews"]:
        failed.append("minimum_human_reviews")
    if invalid:
        failed.append("invalid_records")
    for name, value in metrics.items():
        if value < ACTIVATION_THRESHOLDS[name]:
            failed.append(name)

    return {
        "schema": "memorymaster.dreaming.eval.v1",
        "labeled_decisions": len(valid),
        "human_reviews": len(reviewed),
        "invalid_records": invalid,
        "metrics": metrics,
        "thresholds": ACTIVATION_THRESHOLDS,
        "failed_gates": failed,
        "activation_ready": not failed,
    }
