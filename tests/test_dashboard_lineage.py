from __future__ import annotations

import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from memorymaster.surfaces.dashboard import create_dashboard_server
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


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
        citations=[CitationInput(source="tests/test_dashboard_lineage.py", locator=value, excerpt=text)],
        subject="support",
        predicate="email",
        object_value=value,
        confidence=0.8,
        scope="project:memorymaster",
    )
    return int(claim.id)


def test_claim_lineage_route_renders_supersession_svg(tmp_path: Path) -> None:
    service = MemoryService(tmp_path / "lineage.db", workspace_root=tmp_path)
    service.init_db()
    first = _claim(service, "Support email was old@example.com", "old@example.com")
    second = _claim(service, "Support email changed to new@example.com", "new@example.com")
    third = _claim(service, "Support email changed to current@example.com", "current@example.com")

    service.store.mark_superseded(first, second, "lineage test replacement")
    service.store.mark_superseded(second, third, "lineage test replacement")

    with _running_server(service, tmp_path) as base_url:
        with urllib.request.urlopen(f"{base_url}/claim/{second}/lineage", timeout=5) as response:
            html = response.read().decode("utf-8")
            content_type = response.headers["Content-Type"]

    assert response.status == 200
    assert content_type.startswith("text/html")
    assert "<svg" in html
    assert "Claim Lineage" in html
    assert "replaced_by_claim_id" in html
    assert f"#{first}" in html
    assert f"#{second}" in html
    assert f"#{third}" in html
    assert "old@example.com" in html
    assert "current@example.com" in html
