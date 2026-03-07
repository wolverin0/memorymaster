from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ConfusionCounts:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    def add(self, expected_positive: bool, predicted_positive: bool) -> None:
        if expected_positive and predicted_positive:
            self.tp += 1
        elif not expected_positive and predicted_positive:
            self.fp += 1
        elif not expected_positive and not predicted_positive:
            self.tn += 1
        else:
            self.fn += 1

    def metrics(self) -> dict[str, float]:
        precision = (self.tp / (self.tp + self.fp)) if (self.tp + self.fp) else 0.0
        recall = (self.tp / (self.tp + self.fn)) if (self.tp + self.fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        accuracy = ((self.tp + self.tn) / max(self.tp + self.fp + self.tn + self.fn, 1))
        return {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "accuracy": accuracy,
        }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                rows.append(parsed)
    return rows


def _status(row: dict[str, Any], field: str) -> str:
    value = row.get(field, "")
    if value is None:
        return ""
    return str(value).strip().lower()


def evaluate(rows: list[dict[str, Any]], *, expected_field: str, predicted_field: str) -> dict[str, Any]:
    stale = ConfusionCounts()
    conflicted = ConfusionCounts()
    flagged = ConfusionCounts()

    for row in rows:
        expected_status = _status(row, expected_field)
        predicted_status = _status(row, predicted_field)

        stale.add(expected_status == "stale", predicted_status == "stale")
        conflicted.add(expected_status == "conflicted", predicted_status == "conflicted")
        flagged.add(
            expected_status in {"stale", "conflicted"},
            predicted_status in {"stale", "conflicted"},
        )

    stale_metrics = stale.metrics()
    conflicted_metrics = conflicted.metrics()
    flagged_metrics = flagged.metrics()
    macro_f1 = (stale_metrics["f1"] + conflicted_metrics["f1"] + flagged_metrics["f1"]) / 3.0
    macro_precision = (
        stale_metrics["precision"] + conflicted_metrics["precision"] + flagged_metrics["precision"]
    ) / 3.0
    macro_recall = (stale_metrics["recall"] + conflicted_metrics["recall"] + flagged_metrics["recall"]) / 3.0
    macro_accuracy = (
        stale_metrics["accuracy"] + conflicted_metrics["accuracy"] + flagged_metrics["accuracy"]
    ) / 3.0

    return {
        "rows": len(rows),
        "stale_detection": {"counts": asdict(stale), "metrics": stale_metrics},
        "conflicted_detection": {"counts": asdict(conflicted), "metrics": conflicted_metrics},
        "stale_or_conflicted_detection": {"counts": asdict(flagged), "metrics": flagged_metrics},
        "macro_metrics": {
            "precision": macro_precision,
            "recall": macro_recall,
            "f1": macro_f1,
            "accuracy": macro_accuracy,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute confusion matrices for stale/conflicted status detection quality."
    )
    parser.add_argument(
        "--input-jsonl",
        action="append",
        required=True,
        help="JSONL input with expected/predicted status fields (repeat flag for multiple files).",
    )
    parser.add_argument("--expected-field", default="expected_status", help="Field name for expected status.")
    parser.add_argument("--predicted-field", default="predicted_status", help="Field name for predicted status.")
    parser.add_argument("--out-json", default="artifacts/eval/confusion_matrix.json", help="Output JSON report path.")
    parser.add_argument(
        "--out-csv",
        default="",
        help="Optional CSV path for summary metrics.",
    )
    parser.add_argument("--min-f1-stale", type=float, default=0.0, help="Optional stale F1 threshold.")
    parser.add_argument("--min-f1-conflicted", type=float, default=0.0, help="Optional conflicted F1 threshold.")
    parser.add_argument("--min-f1-macro", type=float, default=0.0, help="Optional macro F1 threshold.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when thresholds are not met.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_paths = [Path(path) for path in args.input_jsonl]
    rows: list[dict[str, Any]] = []
    for path in input_paths:
        rows.extend(load_jsonl(path))
    report = evaluate(rows, expected_field=args.expected_field, predicted_field=args.predicted_field)
    threshold_failures: list[str] = []

    stale_f1 = float(report["stale_detection"]["metrics"]["f1"])
    conflicted_f1 = float(report["conflicted_detection"]["metrics"]["f1"])
    macro_f1 = float(report["macro_metrics"]["f1"])
    if stale_f1 < float(args.min_f1_stale):
        threshold_failures.append(f"stale_f1<{args.min_f1_stale} (actual={stale_f1:.4f})")
    if conflicted_f1 < float(args.min_f1_conflicted):
        threshold_failures.append(f"conflicted_f1<{args.min_f1_conflicted} (actual={conflicted_f1:.4f})")
    if macro_f1 < float(args.min_f1_macro):
        threshold_failures.append(f"macro_f1<{args.min_f1_macro} (actual={macro_f1:.4f})")

    payload = {
        "inputs_jsonl": [str(path) for path in input_paths],
        "expected_field": args.expected_field,
        "predicted_field": args.predicted_field,
        "report": report,
        "threshold_failures": threshold_failures,
        "passed": len(threshold_failures) == 0,
    }

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if str(args.out_csv).strip():
        csv_path = Path(args.out_csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(
            "metric,precision,recall,f1,accuracy\n"
            f"stale_detection,{report['stale_detection']['metrics']['precision']:.6f},{report['stale_detection']['metrics']['recall']:.6f},{report['stale_detection']['metrics']['f1']:.6f},{report['stale_detection']['metrics']['accuracy']:.6f}\n"
            f"conflicted_detection,{report['conflicted_detection']['metrics']['precision']:.6f},{report['conflicted_detection']['metrics']['recall']:.6f},{report['conflicted_detection']['metrics']['f1']:.6f},{report['conflicted_detection']['metrics']['accuracy']:.6f}\n"
            f"stale_or_conflicted_detection,{report['stale_or_conflicted_detection']['metrics']['precision']:.6f},{report['stale_or_conflicted_detection']['metrics']['recall']:.6f},{report['stale_or_conflicted_detection']['metrics']['f1']:.6f},{report['stale_or_conflicted_detection']['metrics']['accuracy']:.6f}\n"
            f"macro,{report['macro_metrics']['precision']:.6f},{report['macro_metrics']['recall']:.6f},{report['macro_metrics']['f1']:.6f},{report['macro_metrics']['accuracy']:.6f}\n",
            encoding="utf-8",
        )
    print(json.dumps(payload, indent=2))

    if args.strict and threshold_failures:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
