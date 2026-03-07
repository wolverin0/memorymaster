from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from memorymaster.metrics_exporter import export_metrics_snapshot

_LATENCY_OPERATIONS = ("ingest", "query", "cycle", "operator_turn")


def _base_report(status: str, events_jsonl: str) -> dict[str, Any]:
    return {
        "status": status,
        "events_jsonl": events_jsonl,
        "total_events": 0,
        "stream_starts": 0,
        "stream_exits": 0,
        "turns_processed": 0,
        "json_errors": 0,
        "state_loaded": 0,
        "state_saved": 0,
        "state_error": 0,
        "reconcile_runs": 0,
        "retrieval_tier_counts": {
            "tier1": 0,
            "tier2": 0,
            "single": 0,
        },
        "latency_ms": {
            op: {
                "count": 0,
                "p50_ms": None,
                "p95_ms": None,
                "max_ms": None,
            }
            for op in _LATENCY_OPERATIONS
        },
        "queue": {
            "samples": 0,
            "last_seen_events": 0,
            "last_processed_events": 0,
            "current_backlog": 0,
            "max_backlog": 0,
        },
        "errors": {
            "json_errors": 0,
            "state_errors": 0,
            "stream_starts": 0,
            "stream_exits": 0,
            "stream_open": 0,
            "total": 0,
            "error_rate": 0.0,
        },
        "avg_extracted_per_turn": 0.0,
        "avg_ingested_per_turn": 0.0,
    }


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        if math.isfinite(parsed):
            return parsed
    except (TypeError, ValueError):
        return None
    return None


def _round_or_none(value: Any, digits: int = 3) -> float | None:
    parsed = _safe_float(value)
    if parsed is None:
        return None
    return round(parsed, digits)


def _enrich_latency(report: dict[str, Any], events_path: Path) -> None:
    snapshot = export_metrics_snapshot(events_jsonl=[events_path])
    latency = snapshot.get("latency_ms")
    if not isinstance(latency, dict):
        return
    for operation in _LATENCY_OPERATIONS:
        row = latency.get(operation, {})
        if not isinstance(row, dict):
            continue
        report["latency_ms"][operation] = {
            "count": _safe_int(row.get("count")),
            "p50_ms": _round_or_none(row.get("p50_ms")),
            "p95_ms": _round_or_none(row.get("p95_ms")),
            "max_ms": _round_or_none(row.get("max_ms")),
        }


def _finalize_backlog(report: dict[str, Any], *, last_seen: int, last_processed: int, samples: int, max_backlog: int) -> None:
    report["queue"] = {
        "samples": max(0, int(samples)),
        "last_seen_events": max(0, int(last_seen)),
        "last_processed_events": max(0, int(last_processed)),
        "current_backlog": max(0, int(last_seen) - int(last_processed)),
        "max_backlog": max(0, int(max_backlog)),
    }


def _finalize_errors(report: dict[str, Any]) -> None:
    stream_open = max(_safe_int(report["stream_starts"]) - _safe_int(report["stream_exits"]), 0)
    total_errors = _safe_int(report["json_errors"]) + _safe_int(report["state_error"]) + stream_open
    total_events = _safe_int(report["total_events"])
    error_rate = (float(total_errors) / float(total_events)) if total_events > 0 else 0.0
    report["errors"] = {
        "json_errors": _safe_int(report["json_errors"]),
        "state_errors": _safe_int(report["state_error"]),
        "stream_starts": _safe_int(report["stream_starts"]),
        "stream_exits": _safe_int(report["stream_exits"]),
        "stream_open": stream_open,
        "total": total_errors,
        "error_rate": round(error_rate, 6),
    }


def _compute_metrics(events_path: Path) -> dict[str, Any]:
    report = _base_report("ok", str(events_path))
    extracted_total = 0
    ingested_total = 0
    last_seen_events = 0
    last_processed_events = 0
    backlog_samples = 0
    max_backlog = 0

    with events_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip().lstrip("\ufeff")
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue

            report["total_events"] += 1
            if "seen_events" in row:
                last_seen_events = max(last_seen_events, _safe_int(row.get("seen_events")))
            if "processed_events" in row:
                last_processed_events = max(last_processed_events, _safe_int(row.get("processed_events")))
            if "seen_events" in row or "processed_events" in row:
                backlog_samples += 1
                max_backlog = max(max_backlog, max(last_seen_events - last_processed_events, 0))
            event = str(row.get("event", "")).strip()

            if event == "stream_start":
                report["stream_starts"] += 1
            elif event == "stream_exit":
                report["stream_exits"] += 1
            elif event == "turn_processed":
                report["turns_processed"] += 1
                extracted_total += _safe_int(row.get("extracted", 0))
                ingested_total += _safe_int(row.get("ingested", 0))
                retrieval_tier = str(row.get("retrieval_tier", "")).strip().lower()
                if retrieval_tier in report["retrieval_tier_counts"]:
                    report["retrieval_tier_counts"][retrieval_tier] += 1
            elif event == "json_error":
                report["json_errors"] += 1
            elif event == "state_loaded":
                report["state_loaded"] += 1
            elif event == "state_saved":
                report["state_saved"] += 1
            elif event == "state_error":
                report["state_error"] += 1
            elif event == "reconcile_run":
                report["reconcile_runs"] += 1

    turns = report["turns_processed"]
    if turns > 0:
        report["avg_extracted_per_turn"] = round(extracted_total / turns, 3)
        report["avg_ingested_per_turn"] = round(ingested_total / turns, 3)

    _enrich_latency(report, events_path)
    _finalize_backlog(
        report,
        last_seen=last_seen_events,
        last_processed=last_processed_events,
        samples=backlog_samples,
        max_backlog=max_backlog,
    )
    _finalize_errors(report)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate MemoryMaster operator JSONL events into summary metrics.")
    parser.add_argument(
        "--events-jsonl",
        default="artifacts/operator/operator_events.jsonl",
        help="Path to operator event log JSONL.",
    )
    parser.add_argument(
        "--out-json",
        default="artifacts/e2e/operator_metrics.json",
        help="Path to output metrics JSON.",
    )
    args = parser.parse_args()

    events_path = Path(args.events_jsonl)
    out_path = Path(args.out_json)

    if not events_path.exists():
        report = _base_report("no_events", str(events_path))
        _write_json(out_path, report)
        print(f"status=no_events total_events=0 out={out_path}")
        return 0

    report = _compute_metrics(events_path)
    _write_json(out_path, report)
    print(
        " ".join(
            [
                f"status={report['status']}",
                f"total_events={report['total_events']}",
                f"turns_processed={report['turns_processed']}",
                f"errors_total={report['errors']['total']}",
                f"queue_max_backlog={report['queue']['max_backlog']}",
                f"out={out_path}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
