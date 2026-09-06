"""Offline usefulness and activation gates for native Dreaming."""

from __future__ import annotations

import json
from collections import Counter
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
    "useful_precision": 0.90,
    "useful_recall": 0.85,
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
    if not isinstance(record, dict):
        return False
    if not isinstance(record.get("record_id"), str) or not record["record_id"].strip():
        return False
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


def _metrics(valid: list[dict]) -> tuple[dict, dict, int]:
    emitted = [row for row in valid if row["emitted"]]
    ephemeral = [row for row in valid if not row["should_emit"]]
    reviewed = [row for row in emitted if row.get("label_origin") == "human"
                and type(row.get("human_accept")) is bool]
    useful = [row for row in valid if row["should_emit"]]
    captured_useful = sum(row["emitted"] for row in useful)

    metrics = {
        "useful_precision": _ratio(captured_useful, len(emitted)),
        "useful_recall": _ratio(captured_useful, len(useful)),
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
    counts = {
        "expected_useful": len(useful), "captured_useful": captured_useful,
        "missed_useful": len(useful) - captured_useful,
        "unwanted_emissions": len(emitted) - captured_useful,
    }
    return metrics, counts, len(reviewed)


def evaluate_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(records)
    invalid = [str(row.get("record_id", index)) if isinstance(row, dict) else str(index)
               for index, row in enumerate(rows) if not _valid_record(row)]
    valid = [row for row in rows if _valid_record(row)]
    duplicates = sorted(key for key, count in Counter(row["record_id"] for row in valid).items() if count > 1)
    metrics, counts, reviewed_count = _metrics(valid)
    failed: list[str] = []
    if len(valid) < ACTIVATION_THRESHOLDS["minimum_labeled_decisions"]:
        failed.append("minimum_labeled_decisions")
    if reviewed_count < ACTIVATION_THRESHOLDS["minimum_human_reviews"]:
        failed.append("minimum_human_reviews")
    if invalid:
        failed.append("invalid_records")
    if duplicates:
        failed.append("duplicate_record_ids")
    for name, value in metrics.items():
        if value < ACTIVATION_THRESHOLDS[name]:
            failed.append(name)

    return {
        "schema": "memorymaster.dreaming.eval.v1",
        "labeled_decisions": len(valid),
        "human_reviews": reviewed_count,
        "invalid_records": invalid,
        "duplicate_record_ids": duplicates,
        "counts": counts,
        "metrics": metrics,
        "thresholds": dict(ACTIVATION_THRESHOLDS),
        "failed_gates": failed,
        "activation_ready": not failed,
    }
