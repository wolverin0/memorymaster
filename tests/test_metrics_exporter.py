from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from memorymaster.metrics_exporter import export_metrics, export_metrics_snapshot, render_prometheus_text


def _tmp_file(prefix: str, suffix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=suffix, dir=".tmp_cases")
    os.close(fd)
    return Path(raw)


def test_export_metrics_snapshot_and_prometheus_output() -> None:
    events_jsonl = _tmp_file("metrics-events", ".jsonl")
    out_json = _tmp_file("metrics-out", ".json")
    out_prom = _tmp_file("metrics-out", ".prom")

    rows = [
        {"event_type": "ingest", "latency_ms": 12.0, "status": "candidate"},
        {"event_type": "query", "duration_seconds": 0.2, "status": "confirmed"},
        {"event_type": "cycle", "duration_ms": 1500.0, "to_status": "confirmed"},
        {"event": "turn_processed", "duration_ms": 80.0},
        {"event": "turn_processed", "duration_ms": 120.0},
        {"event_type": "transition", "from_status": "candidate", "to_status": "confirmed"},
        {"event_type": "transition", "from_status": "confirmed", "to_status": "stale"},
        {"event": "custom_event", "status": "stale"},
    ]
    events_jsonl.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    snapshot = export_metrics(
        events_jsonl=[events_jsonl],
        out_prom=out_prom,
        out_json=out_json,
    )

    assert snapshot["rows"]["parsed_events"] == 8
    assert snapshot["rows"]["invalid_json_lines"] == 0
    assert snapshot["counters"]["events_total"] == 8
    assert snapshot["counters"]["transitions_total"] == 2

    latency = snapshot["latency_ms"]
    assert latency["ingest"]["count"] == 1
    assert latency["ingest"]["p50_ms"] == 12.0
    assert latency["query"]["p95_ms"] == 200.0
    assert latency["cycle"]["p50_ms"] == 1500.0
    assert latency["operator_turn"]["count"] == 2
    assert latency["operator_turn"]["p50_ms"] == 80.0
    assert latency["operator_turn"]["p95_ms"] == 120.0

    prom_text = out_prom.read_text(encoding="utf-8")
    assert 'memorymaster_events_total{event="turn_processed"} 2' in prom_text
    assert 'memorymaster_transitions_total{from_status="candidate",to_status="confirmed"} 1' in prom_text
    assert 'memorymaster_latency_p95_ms{operation="operator_turn"} 120.000000' in prom_text

    written_snapshot = json.loads(out_json.read_text(encoding="utf-8"))
    assert written_snapshot["schema_version"] == "d3_metrics_export_v1"
    assert written_snapshot["series"]["event_counts"]


def test_export_metrics_snapshot_handles_invalid_lines_and_missing_files() -> None:
    events_jsonl = _tmp_file("metrics-events-invalid", ".jsonl")
    missing_jsonl = events_jsonl.with_name(events_jsonl.stem + "-missing.jsonl")
    events_jsonl.write_text('{"event":"ok"}\nnot-json\n', encoding="utf-8")

    snapshot = export_metrics_snapshot([events_jsonl, missing_jsonl])
    prom = render_prometheus_text(snapshot)

    assert snapshot["rows"]["total_non_empty"] == 2
    assert snapshot["rows"]["parsed_events"] == 1
    assert snapshot["rows"]["invalid_json_lines"] == 1
    assert str(missing_jsonl) in snapshot["sources"]["missing_files"]
    assert "memorymaster_export_invalid_json_lines_total 1" in prom
    assert 'memorymaster_events_total{event="ok"} 1' in prom


def test_cli_export_metrics_writes_outputs() -> None:
    events_jsonl = _tmp_file("metrics-cli-events", ".jsonl")
    out_json = _tmp_file("metrics-cli-out", ".json")
    out_prom = _tmp_file("metrics-cli-out", ".prom")

    events_jsonl.write_text('{"event_type":"ingest","latency_ms":5.5}\n', encoding="utf-8")

    cmd = [
        sys.executable,
        "-m",
        "memorymaster",
        "export-metrics",
        "--events-jsonl",
        str(events_jsonl),
        "--out-prom",
        str(out_prom),
        "--out-json",
        str(out_json),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr

    payload = json.loads(proc.stdout)
    assert payload["command"] == "export-metrics"
    assert payload["events_total"] == 1
    assert payload["out_prom"] == str(out_prom)
    assert payload["out_json"] == str(out_json)

    assert out_json.exists()
    assert out_prom.exists()

