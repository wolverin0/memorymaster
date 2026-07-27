"""Capture Inbox dashboard and retirement interaction contracts.

The inbox must expose source-to-evidence-to-claim-to-relationship lineage,
show durable job diagnostics, and keep source retirement preview-only until
the operator explicitly applies it. Evidence and audit history remain after
retirement.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from memorymaster import remember
from memorymaster.capture import CaptureRepository
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.knowledge.entity_graph import EntityGraph
from memorymaster.surfaces.capture_inbox import capture_inbox_payload
from memorymaster.surfaces.dashboard import create_dashboard_server


@contextmanager
def _server(service: MemoryService, db: Path, workspace: Path) -> Iterator[str]:
    server = create_dashboard_server(
        service=service,
        db_target=db,
        workspace_root=workspace,
        host="127.0.0.1",
        port=0,
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


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _fixture(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = tmp_path / "inbox.db"
    receipt = remember(
        text="Alice participates in Project Atlas.",
        scope="project:inbox",
        db=db,
        workspace=workspace,
    )
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    evidence_id = int(receipt.evidence["id"])
    claim = service.ingest(
        "Alice participates in Project Atlas.",
        [CitationInput(source="fixture", locator=f"evidence:{evidence_id}")],
        scope="project:inbox",
    )
    CaptureRepository(service.store).link_claim_evidence(
        claim_id=claim.id, evidence_item_id=evidence_id
    )
    claim = service.store.apply_status_transition(
        claim,
        to_status="confirmed",
        reason="inbox fixture",
        event_type="validator",
    )
    payload = json.dumps(
        {
            "entities": [
                {"name": "Alice", "type": "person", "aliases": []},
                {"name": "Project Atlas", "type": "project", "aliases": []},
            ],
            "relations": [
                {
                    "source": "Alice",
                    "target": "Project Atlas",
                    "relation": "participates_in",
                }
            ],
        }
    )
    with patch("memorymaster.knowledge.entity_graph._llm_chat", return_value=payload):
        EntityGraph(str(db)).extract_and_link(claim.id, claim.text)
    return service, db, workspace, receipt, claim


def test_capture_inbox_read_model_exposes_complete_lineage(tmp_path: Path) -> None:
    service, _, _, receipt, claim = _fixture(tmp_path)

    payload = capture_inbox_payload(service)

    item = payload["items"][0]
    assert item["id"] == receipt.source_item["id"]
    assert item["jobs"][0]["stage"] == "extract_claims"
    assert item["evidence"][0]["id"] == receipt.evidence["id"]
    assert item["claims"][0]["id"] == claim.id
    assert item["claims"][0]["citations"]
    assert item["relationships"][0] == {
        "source": "Alice",
        "target": "Project Atlas",
        "relation": "participates_in",
        "supporting_claim_id": claim.id,
        "scope": "project:inbox",
        "ontology_version": "personal-v1",
    }


def test_capture_inbox_masks_sensitive_claim_text(tmp_path: Path) -> None:
    service, _, _, _, claim = _fixture(tmp_path)
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE claims SET visibility = ? WHERE id = ?",
            ("sensitive", claim.id),
        )
        conn.commit()

    payload = capture_inbox_payload(service)

    assert payload["items"][0]["claims"][0]["text"] == "[sensitive claim]"


def test_dashboard_retirement_is_preview_then_apply(tmp_path: Path) -> None:
    service, db, workspace, receipt, claim = _fixture(tmp_path)
    source_id = int(receipt.source_item["id"])

    with _server(service, db, workspace) as base_url:
        preview = _post(
            f"{base_url}/api/capture-inbox/retire",
            {"source_item_id": source_id, "apply": False},
        )
        assert preview["apply"] is False
        assert service.get_source_item_by_id(source_id).retired_at is None

        applied = _post(
            f"{base_url}/api/capture-inbox/retire",
            {"source_item_id": source_id, "apply": True},
        )
        assert applied["apply"] is True
        with urllib.request.urlopen(
            f"{base_url}/api/capture-inbox", timeout=3
        ) as response:
            inbox = json.loads(response.read().decode("utf-8"))

    assert inbox["items"][0]["retired_at"]
    assert inbox["items"][0]["evidence"]
    assert service.store.get_claim(claim.id).status == "stale"
