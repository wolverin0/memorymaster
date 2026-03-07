from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from memorymaster.dashboard import create_dashboard_server
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


@contextmanager
def _running_server(service: MemoryService, operator_log_jsonl: Path) -> Iterator[str]:
    server = create_dashboard_server(
        service=service,
        host="127.0.0.1",
        port=0,
        operator_log_jsonl=operator_log_jsonl,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get_json(url: str) -> tuple[int, dict[str, str], dict[str, object]]:
    with urllib.request.urlopen(url, timeout=3) as response:
        status = int(response.status)
        headers = {k: v for k, v in response.headers.items()}
        payload = json.loads(response.read().decode("utf-8"))
    return status, headers, payload


def test_dashboard_health_and_html() -> None:
    db = _case_db("sqlite-dashboard-health")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    operator_log = Path(".tmp_cases") / "dashboard-health-operator.jsonl"
    operator_log.parent.mkdir(parents=True, exist_ok=True)
    operator_log.write_text("", encoding="utf-8")

    with _running_server(service, operator_log) as base_url:
        status, headers, payload = _get_json(f"{base_url}/health")
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert payload["ok"] is True

        with urllib.request.urlopen(f"{base_url}/dashboard", timeout=3) as response:
            assert int(response.status) == 200
            assert response.headers["Content-Type"].startswith("text/html")
            html = response.read().decode("utf-8")
        assert "MemoryMaster Dashboard" in html
        assert "Claims Table" in html
        assert "Timeline Feed" in html
        assert "Conflict Comparisons" in html
        assert "/api/claims?limit=50" in html
        assert "/api/operator/stream" in html
        assert "JSON.stringify(data, null, 2)" not in html


def test_dashboard_data_endpoints() -> None:
    db = _case_db("sqlite-dashboard-data")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    service.ingest(
        text="Support email is old@example.com",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="old value")],
        subject="support",
        predicate="email",
        object_value="old@example.com",
        confidence=0.8,
    )
    service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.4)
    service.ingest(
        text="Correction support email is new@example.com",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="new value")],
        subject="support",
        predicate="email",
        object_value="new@example.com",
        confidence=0.75,
    )
    service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.4)

    operator_log = Path(".tmp_cases") / "dashboard-data-operator.jsonl"
    operator_log.parent.mkdir(parents=True, exist_ok=True)
    operator_log.write_text("", encoding="utf-8")

    with _running_server(service, operator_log) as base_url:
        claims_status, _, claims_payload = _get_json(f"{base_url}/api/claims?limit=10")
        assert claims_status == 200
        assert int(claims_payload["rows"]) >= 2
        claims = claims_payload["claims"]
        assert isinstance(claims, list)
        assert any(claim["predicate"] == "email" for claim in claims)
        first_claim = claims[0]
        assert isinstance(first_claim["citations"], list)
        assert {"id", "status", "confidence", "updated_at", "subject", "predicate", "text"} <= set(first_claim)

        events_status, _, events_payload = _get_json(f"{base_url}/api/events?limit=20")
        assert events_status == 200
        assert int(events_payload["rows"]) >= 1
        events = events_payload["events"]
        assert isinstance(events, list)
        assert "event_type" in events[0]
        assert "payload" in events[0]

        timeline_status, _, timeline_payload = _get_json(f"{base_url}/api/timeline?limit=20")
        assert timeline_status == 200
        assert int(timeline_payload["rows"]) >= 1
        timeline = timeline_payload["timeline"]
        assert isinstance(timeline, list)
        assert "event_type" in timeline[0]
        assert {"created_at", "from_status", "to_status"} <= set(timeline[0])

        conflicts_status, _, conflicts_payload = _get_json(f"{base_url}/api/conflicts?limit=20")
        assert conflicts_status == 200
        assert int(conflicts_payload["rows"]) >= 1
        groups = conflicts_payload["groups"]
        assert isinstance(groups, list)
        first_group = groups[0]
        assert "subject" in first_group
        assert "predicate" in first_group
        assert "claims" in first_group
        assert isinstance(first_group["claims"], list)
        assert first_group["claims"]
        first_conflict_claim = first_group["claims"][0]
        assert {"status", "updated_at", "citations"} <= set(first_conflict_claim)
        assert isinstance(first_conflict_claim["citations"], list)
        if len(first_group["claims"]) >= 2:
            assert str(first_group["claims"][0]["updated_at"]) >= str(first_group["claims"][1]["updated_at"])

        review_status, _, review_payload = _get_json(f"{base_url}/api/review-queue?limit=20")
        assert review_status == 200
        assert int(review_payload["rows"]) >= 1
        items = review_payload["items"]
        assert isinstance(items, list)
        assert any(item["status"] in {"conflicted", "stale"} for item in items)
        assert {"claim_id", "priority", "reason", "citations_count"} <= set(items[0])


def test_dashboard_operator_stream_sse_replays_log() -> None:
    db = _case_db("sqlite-dashboard-stream")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    operator_log = Path(".tmp_cases") / "dashboard-stream-operator.jsonl"
    operator_log.parent.mkdir(parents=True, exist_ok=True)
    event_row = {
        "ts": "2026-03-03T00:00:00+00:00",
        "event": "turn_processed",
        "turn_id": "turn-1",
        "processed_events": 1,
    }
    operator_log.write_text(json.dumps(event_row, ensure_ascii=True) + "\n", encoding="utf-8")

    with _running_server(service, operator_log) as base_url:
        parsed = urllib.parse.urlparse(base_url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=4)
        conn.request("GET", "/api/operator/stream?last=1&follow=0")
        response = conn.getresponse()
        assert response.status == 200
        assert str(response.headers.get("Content-Type", "")).startswith("text/event-stream")
        lines: list[str] = []
        deadline = time.time() + 2.0
        while time.time() < deadline:
            raw = response.fp.readline()
            if not raw:
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            lines.append(line)
            if line == "" and any(item.startswith("data: ") for item in lines):
                break
        conn.close()

        assert "event: turn_processed" in lines
        data_lines = [line for line in lines if line.startswith("data: ")]
        assert data_lines
        streamed_payload = json.loads(data_lines[0][len("data: ") :])
        assert streamed_payload["turn_id"] == "turn-1"
        assert streamed_payload["processed_events"] == 1

