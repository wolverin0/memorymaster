from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

_LATENCY_OPERATIONS = ("ingest", "query", "cycle", "operator_turn")

_EXPLICIT_LATENCY_FIELDS_MS: dict[str, tuple[str, ...]] = {
    "ingest": (
        "ingest_latency_ms",
        "ingest_duration_ms",
        "ingest_elapsed_ms",
    ),
    "query": (
        "query_latency_ms",
        "query_duration_ms",
        "query_elapsed_ms",
    ),
    "cycle": (
        "cycle_latency_ms",
        "cycle_duration_ms",
        "cycle_elapsed_ms",
    ),
    "operator_turn": (
        "operator_turn_latency_ms",
        "operator_turn_duration_ms",
        "operator_turn_elapsed_ms",
        "turn_latency_ms",
        "turn_duration_ms",
        "turn_elapsed_ms",
    ),
}

_EXPLICIT_LATENCY_FIELDS_SECONDS: dict[str, tuple[str, ...]] = {
    "ingest": (
        "ingest_latency_seconds",
        "ingest_duration_seconds",
        "ingest_elapsed_seconds",
    ),
    "query": (
        "query_latency_seconds",
        "query_duration_seconds",
        "query_elapsed_seconds",
    ),
    "cycle": (
        "cycle_latency_seconds",
        "cycle_duration_seconds",
        "cycle_elapsed_seconds",
    ),
    "operator_turn": (
        "operator_turn_latency_seconds",
        "operator_turn_duration_seconds",
        "operator_turn_elapsed_seconds",
        "turn_latency_seconds",
        "turn_duration_seconds",
        "turn_elapsed_seconds",
    ),
}

_GENERIC_LATENCY_FIELDS_MS = (
    "latency_ms",
    "duration_ms",
    "elapsed_ms",
)

_GENERIC_LATENCY_FIELDS_SECONDS = (
    "latency_seconds",
    "duration_seconds",
    "elapsed_seconds",
    "runtime_seconds",
)

_PROM_HEADER = """# HELP memorymaster_events_total Count of observed events grouped by event label.
# TYPE memorymaster_events_total counter
# HELP memorymaster_transitions_total Count of observed state transitions grouped by from/to status.
# TYPE memorymaster_transitions_total counter
# HELP memorymaster_status_total Count of observed status occurrences.
# TYPE memorymaster_status_total counter
# HELP memorymaster_latency_samples_total Number of latency samples grouped by operation.
# TYPE memorymaster_latency_samples_total counter
# HELP memorymaster_latency_p50_ms p50 latency in milliseconds grouped by operation.
# TYPE memorymaster_latency_p50_ms gauge
# HELP memorymaster_latency_p95_ms p95 latency in milliseconds grouped by operation.
# TYPE memorymaster_latency_p95_ms gauge
# HELP memorymaster_export_rows_total Number of non-empty JSONL rows processed by the exporter.
# TYPE memorymaster_export_rows_total gauge
# HELP memorymaster_export_invalid_json_lines_total Number of invalid JSONL rows skipped by the exporter.
# TYPE memorymaster_export_invalid_json_lines_total gauge
"""


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        return None
    return None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return ordered[rank - 1]


def _normalize_status(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _parse_json_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _operation_hint(record: dict[str, Any]) -> str | None:
    raw_tokens = [
        str(record.get("operation", "")),
        str(record.get("op", "")),
        str(record.get("event", "")),
        str(record.get("event_type", "")),
        str(record.get("details", "")),
    ]
    text = " ".join(token.strip().lower() for token in raw_tokens if token)
    if not text:
        return None
    if "operator_turn" in text or "turn_processed" in text or "turn_process" in text:
        return "operator_turn"
    if "ingest" in text:
        return "ingest"
    if "query" in text or "retrieval" in text:
        return "query"
    if "cycle" in text or "run_cycle" in text or "reconcile" in text:
        return "cycle"
    return None


def _extract_latency_ms_from_record(operation: str, record: dict[str, Any]) -> float | None:
    for field in _EXPLICIT_LATENCY_FIELDS_MS[operation]:
        value = _safe_float(record.get(field))
        if value is not None and value >= 0:
            return value
    for field in _EXPLICIT_LATENCY_FIELDS_SECONDS[operation]:
        value = _safe_float(record.get(field))
        if value is not None and value >= 0:
            return value * 1000.0
    return None


def _extract_generic_latency_ms(record: dict[str, Any]) -> float | None:
    for field in _GENERIC_LATENCY_FIELDS_MS:
        value = _safe_float(record.get(field))
        if value is not None and value >= 0:
            return value
    for field in _GENERIC_LATENCY_FIELDS_SECONDS:
        value = _safe_float(record.get(field))
        if value is not None and value >= 0:
            return value * 1000.0
    return None


def _extract_duration_from_timestamps(record: dict[str, Any]) -> float | None:
    start = _parse_iso_datetime(record.get("turn_started_at")) or _parse_iso_datetime(record.get("started_at"))
    end = _parse_iso_datetime(record.get("turn_finished_at")) or _parse_iso_datetime(record.get("finished_at"))
    if start is None or end is None:
        return None
    duration_ms = (end - start).total_seconds() * 1000.0
    if duration_ms < 0:
        return None
    return duration_ms


def _collect_latencies(record: dict[str, Any], latency_values: dict[str, list[float]]) -> None:
    payload = _parse_json_dict(record.get("payload_json"))
    contexts = [record]
    if payload is not None:
        contexts.append(payload)

    explicit_seen: set[str] = set()
    for operation in _LATENCY_OPERATIONS:
        for context in contexts:
            value = _extract_latency_ms_from_record(operation, context)
            if value is not None:
                latency_values[operation].append(value)
                explicit_seen.add(operation)
                break

    hinted_operation = _operation_hint(record)
    if hinted_operation and hinted_operation not in explicit_seen:
        generic_value = _extract_generic_latency_ms(record)
        if generic_value is None and payload is not None:
            generic_value = _extract_generic_latency_ms(payload)
        if generic_value is None:
            generic_value = _extract_duration_from_timestamps(record)
        if generic_value is not None:
            latency_values[hinted_operation].append(generic_value)


def _collect_status_and_transitions(
    record: dict[str, Any],
    status_counts: Counter[str],
    transition_counts: Counter[tuple[str, str]],
) -> None:
    payload = _parse_json_dict(record.get("payload_json"))

    status = _normalize_status(record.get("status"))
    if status:
        status_counts[status] += 1

    claim_payload = record.get("claim")
    if isinstance(claim_payload, dict):
        claim_status = _normalize_status(claim_payload.get("status"))
        if claim_status:
            status_counts[claim_status] += 1

    from_status = _normalize_status(record.get("from_status"))
    to_status = _normalize_status(record.get("to_status"))

    if payload is not None:
        if from_status is None:
            from_status = _normalize_status(payload.get("from_status") or payload.get("previous_status"))
        if to_status is None:
            to_status = _normalize_status(payload.get("to_status") or payload.get("new_status") or payload.get("status"))

    if to_status:
        status_counts[to_status] += 1

    if from_status and to_status:
        transition_counts[(from_status, to_status)] += 1


def _event_name(record: dict[str, Any]) -> str:
    event = record.get("event")
    if isinstance(event, str) and event.strip():
        return event.strip()
    event_type = record.get("event_type")
    if isinstance(event_type, str) and event_type.strip():
        return event_type.strip()
    return "unknown"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _build_latency_summary(values: list[float]) -> dict[str, float | int | None]:
    count = len(values)
    if count == 0:
        return {
            "count": 0,
            "min_ms": None,
            "max_ms": None,
            "p50_ms": None,
            "p95_ms": None,
        }
    return {
        "count": count,
        "min_ms": min(values),
        "max_ms": max(values),
        "p50_ms": _percentile(values, 50),
        "p95_ms": _percentile(values, 95),
    }


def export_metrics_snapshot(events_jsonl: list[str | Path]) -> dict[str, Any]:
    paths = [Path(path) for path in events_jsonl]
    missing_files = [str(path) for path in paths if not path.exists()]

    event_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    transition_counts: Counter[tuple[str, str]] = Counter()
    latency_values: dict[str, list[float]] = {operation: [] for operation in _LATENCY_OPERATIONS}

    non_empty_rows = 0
    parsed_events = 0
    invalid_json_lines = 0

    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip().lstrip("\ufeff")
                if not line:
                    continue
                non_empty_rows += 1
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    invalid_json_lines += 1
                    continue
                if not isinstance(parsed, dict):
                    invalid_json_lines += 1
                    continue

                parsed_events += 1
                event_counts[_event_name(parsed)] += 1
                _collect_status_and_transitions(parsed, status_counts, transition_counts)
                _collect_latencies(parsed, latency_values)

    event_counts_series = [
        {"event": event, "count": int(event_counts[event])}
        for event in sorted(event_counts.keys())
    ]
    transition_counts_series = [
        {
            "from_status": from_status,
            "to_status": to_status,
            "count": int(transition_counts[(from_status, to_status)]),
        }
        for from_status, to_status in sorted(transition_counts.keys())
    ]
    status_counts_series = [
        {"status": status, "count": int(status_counts[status])}
        for status in sorted(status_counts.keys())
    ]

    latency_summary = {
        operation: _build_latency_summary(latency_values[operation])
        for operation in _LATENCY_OPERATIONS
    }
    latency_series = [
        {
            "operation": operation,
            "count": int(latency_summary[operation]["count"]),
            "min_ms": latency_summary[operation]["min_ms"],
            "max_ms": latency_summary[operation]["max_ms"],
            "p50_ms": latency_summary[operation]["p50_ms"],
            "p95_ms": latency_summary[operation]["p95_ms"],
        }
        for operation in _LATENCY_OPERATIONS
    ]

    return {
        "schema_version": "d3_metrics_export_v1",
        "sources": {
            "events_jsonl": [str(path) for path in paths],
            "missing_files": missing_files,
        },
        "rows": {
            "total_non_empty": non_empty_rows,
            "parsed_events": parsed_events,
            "invalid_json_lines": invalid_json_lines,
        },
        "counters": {
            "events_total": int(sum(event_counts.values())),
            "transitions_total": int(sum(transition_counts.values())),
            "status_total": int(sum(status_counts.values())),
        },
        "series": {
            "event_counts": event_counts_series,
            "transition_counts": transition_counts_series,
            "status_counts": status_counts_series,
            "latency_ms": latency_series,
        },
        "latency_ms": latency_summary,
    }


def render_prometheus_text(snapshot: dict[str, Any]) -> str:
    lines: list[str] = [line for line in _PROM_HEADER.strip().splitlines()]

    rows = snapshot.get("rows", {})
    lines.append(f"memorymaster_export_rows_total {int(rows.get('total_non_empty', 0))}")
    lines.append(
        "memorymaster_export_invalid_json_lines_total "
        f"{int(rows.get('invalid_json_lines', 0))}"
    )

    series = snapshot.get("series", {})

    for row in series.get("event_counts", []):
        event = _escape_label(str(row.get("event", "unknown")))
        count = int(row.get("count", 0))
        lines.append(f'memorymaster_events_total{{event="{event}"}} {count}')

    for row in series.get("transition_counts", []):
        from_status = _escape_label(str(row.get("from_status", "unknown")))
        to_status = _escape_label(str(row.get("to_status", "unknown")))
        count = int(row.get("count", 0))
        lines.append(
            "memorymaster_transitions_total"
            f'{{from_status="{from_status}",to_status="{to_status}"}} {count}'
        )

    for row in series.get("status_counts", []):
        status = _escape_label(str(row.get("status", "unknown")))
        count = int(row.get("count", 0))
        lines.append(f'memorymaster_status_total{{status="{status}"}} {count}')

    latency = snapshot.get("latency_ms", {})
    for operation in _LATENCY_OPERATIONS:
        item = latency.get(operation, {})
        count = int(item.get("count", 0))
        op = _escape_label(operation)
        lines.append(f'memorymaster_latency_samples_total{{operation="{op}"}} {count}')
        p50 = item.get("p50_ms")
        p95 = item.get("p95_ms")
        if p50 is not None:
            lines.append(f'memorymaster_latency_p50_ms{{operation="{op}"}} {float(p50):.6f}')
        if p95 is not None:
            lines.append(f'memorymaster_latency_p95_ms{{operation="{op}"}} {float(p95):.6f}')

    return "\n".join(lines) + "\n"


def export_metrics(
    *,
    events_jsonl: list[str | Path],
    out_prom: str | Path,
    out_json: str | Path,
) -> dict[str, Any]:
    snapshot = export_metrics_snapshot(events_jsonl=list(events_jsonl))

    out_json_path = Path(out_json)
    out_prom_path = Path(out_prom)
    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    out_prom_path.parent.mkdir(parents=True, exist_ok=True)

    out_json_path.write_text(json.dumps(snapshot, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    out_prom_path.write_text(render_prometheus_text(snapshot), encoding="utf-8")

    return snapshot
