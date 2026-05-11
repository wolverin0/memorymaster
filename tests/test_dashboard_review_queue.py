from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from memorymaster.dashboard import create_dashboard_server
from memorymaster.service import MemoryService


def _case_db(prefix: str) -> Path:
    Path(".tmp_cases").mkdir(parents=True, exist_ok=True)
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
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _service(prefix: str) -> MemoryService:
    service = MemoryService(_case_db(prefix), workspace_root=Path.cwd())
    service.init_db()
    return service


def _operator_log(prefix: str) -> Path:
    path = Path(".tmp_cases") / f"{prefix}-operator.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _get_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _seed_claims(service: MemoryService, count: int, *, scope: str = "project") -> list[int]:
    ids: list[int] = []
    for index in range(count):
        claim = service.ingest(
            text=f"Claim {scope} {index}",
            citations=[],
            claim_type="fact",
            scope=scope,
            confidence=0.5 + (index / 1000),
        )
        ids.append(int(claim.id))
    return ids


def test_review_queue_empty_queue() -> None:
    service = _service("dashboard-review-empty")
    with _running_server(service, _operator_log("dashboard-review-empty")) as base_url:
        payload = _get_json(f"{base_url}/api/v1/review-queue")

    assert payload == {"queue": [], "cursor": None}


def test_review_queue_n_five() -> None:
    service = _service("dashboard-review-five")
    ids = _seed_claims(service, 6)

    with _running_server(service, _operator_log("dashboard-review-five")) as base_url:
        payload = _get_json(f"{base_url}/api/v1/review-queue?n=5")

    queue = payload["queue"]
    assert isinstance(queue, list)
    assert [item["id"] for item in queue] == ids[:5]
    assert payload["cursor"] == ids[4]
    assert queue[0]["text_preview"] == "Claim project 0"
    assert {"id", "text_preview", "age_days", "scope", "type", "score"} == set(queue[0])


def test_review_queue_n_caps_at_one_hundred() -> None:
    service = _service("dashboard-review-cap")
    ids = _seed_claims(service, 101)

    with _running_server(service, _operator_log("dashboard-review-cap")) as base_url:
        payload = _get_json(f"{base_url}/api/v1/review-queue?n=250")

    queue = payload["queue"]
    assert isinstance(queue, list)
    assert len(queue) == 100
    assert [item["id"] for item in queue] == ids[:100]
    assert payload["cursor"] == ids[99]


def test_review_queue_scope_filter() -> None:
    service = _service("dashboard-review-scope")
    _seed_claims(service, 2, scope="project:alpha")
    beta_ids = _seed_claims(service, 3, scope="project:beta")
    scope = urllib.parse.quote("project:beta", safe="")

    with _running_server(service, _operator_log("dashboard-review-scope")) as base_url:
        payload = _get_json(f"{base_url}/api/v1/review-queue?scope={scope}&n=10")

    queue = payload["queue"]
    assert isinstance(queue, list)
    assert [item["id"] for item in queue] == beta_ids
    assert {item["scope"] for item in queue} == {"project:beta"}
    assert payload["cursor"] is None


def test_review_queue_cursor_pagination() -> None:
    service = _service("dashboard-review-cursor")
    ids = _seed_claims(service, 7)

    with _running_server(service, _operator_log("dashboard-review-cursor")) as base_url:
        first = _get_json(f"{base_url}/api/v1/review-queue?n=3")
        second = _get_json(f"{base_url}/api/v1/review-queue?n=3&cursor={first['cursor']}")
        third = _get_json(f"{base_url}/api/v1/review-queue?n=3&cursor={second['cursor']}")

    assert [item["id"] for item in first["queue"]] == ids[:3]
    assert first["cursor"] == ids[2]
    assert [item["id"] for item in second["queue"]] == ids[3:6]
    assert second["cursor"] == ids[5]
    assert [item["id"] for item in third["queue"]] == ids[6:]
    assert third["cursor"] is None
