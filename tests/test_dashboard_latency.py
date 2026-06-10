from __future__ import annotations

import json
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from memorymaster.surfaces.dashboard import create_dashboard_server
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


@contextmanager
def _running_server(service: MemoryService, tmp_path: Path) -> Iterator[str]:
    server = create_dashboard_server(
        service=service,
        host="127.0.0.1",
        port=0,
        operator_log_jsonl=tmp_path / "operator.jsonl",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _claim(service: MemoryService, text: str, value: str) -> int:
    claim = service.ingest(
        text=text,
        citations=[CitationInput(source="tests/test_dashboard_latency.py", locator=value, excerpt=text)],
        subject="latency",
        predicate="sample",
        object_value=value,
        confidence=0.8,
        scope="project:memorymaster",
    )
    return int(claim.id)


def _set_claim_created_at(service: MemoryService, claim_id: int, created_at: str) -> None:
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE claims SET created_at = ?, updated_at = ? WHERE id = ?",
            (created_at, created_at, claim_id),
        )
        conn.commit()


def _insert_validation_event(service: MemoryService, claim_id: int, event_type: str, created_at: str) -> None:
    with service.store.connect() as conn:
        conn.execute(
            """
            INSERT INTO events (claim_id, event_type, from_status, to_status, details, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (claim_id, event_type, "candidate", "confirmed", "test validation", "{}", created_at),
        )
        conn.commit()


def test_validation_latency_route_returns_percentiles_from_first_validation_event(tmp_path: Path) -> None:
    service = MemoryService(tmp_path / "latency.db", workspace_root=tmp_path)
    service.init_db()

    claim_10 = _claim(service, "Claim validates in ten seconds", "ten")
    claim_20 = _claim(service, "Claim validates in twenty seconds", "twenty")
    claim_40 = _claim(service, "Claim validates in forty seconds", "forty")
    claim_unvalidated = _claim(service, "Claim has no validation event", "none")

    base = "2026-01-01T00:00:00+00:00"
    for claim_id in (claim_10, claim_20, claim_40, claim_unvalidated):
        _set_claim_created_at(service, claim_id, base)
    _insert_validation_event(service, claim_10, "validator", "2026-01-01T00:00:10+00:00")
    _insert_validation_event(service, claim_20, "validator", "2026-01-01T00:01:30+00:00")
    _insert_validation_event(service, claim_20, "deterministic_validator", "2026-01-01T00:00:20+00:00")
    _insert_validation_event(service, claim_40, "deterministic_validator", "2026-01-01T00:00:40+00:00")

    with _running_server(service, tmp_path) as base_url:
        with urllib.request.urlopen(f"{base_url}/metrics/validation-latency", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

    assert response.status == 200
    assert payload["ok"] is True
    assert payload["metric"] == "validation_latency"
    assert payload["unit"] == "seconds"
    assert payload["rows"] == 3
    assert payload["p50"] == pytest.approx(20.0, abs=0.01)
    assert payload["p95"] == pytest.approx(38.0, abs=0.01)
    assert payload["p99"] == pytest.approx(39.6, abs=0.01)


def test_dashboard_index_contains_validation_latency_panel(tmp_path: Path) -> None:
    service = MemoryService(tmp_path / "latency-index.db", workspace_root=tmp_path)
    service.init_db()

    with _running_server(service, tmp_path) as base_url:
        with urllib.request.urlopen(f"{base_url}/dashboard", timeout=5) as response:
            html = response.read().decode("utf-8")

    assert response.status == 200
    assert "Validation Latency" in html
    assert "/metrics/validation-latency" in html
