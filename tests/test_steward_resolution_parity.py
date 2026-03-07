from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from memorymaster.dashboard import create_dashboard_server
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService
from memorymaster.steward import list_steward_proposals, run_steward


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def _case_workspace(prefix: str) -> Path:
    base = Path(".tmp_pytest")
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=str(base)))


def _force_status(db: Path, claim_id: int, status: str, updated_at: str) -> None:
    """Force a claim status for test seeding, bypassing uniqueness guards."""
    con = sqlite3.connect(str(db))
    con.execute("DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_update")
    con.execute("DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_insert")
    con.execute("DROP INDEX IF EXISTS idx_claims_confirmed_tuple_unique")
    con.execute(
        "UPDATE claims SET status=?, updated_at=?, last_validated_at=? WHERE id=?",
        (status, updated_at, updated_at, claim_id),
    )
    con.commit()
    con.close()


def _seed_proposal(service: MemoryService, db: Path, workspace: Path) -> int:
    old_claim = service.ingest(
        text="API endpoint is https://old.example.com",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="old")],
        subject="api",
        predicate="url",
        object_value="https://old.example.com",
        confidence=0.7,
    )
    new_claim = service.ingest(
        text="API endpoint is https://new.example.com",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="new")],
        subject="api",
        predicate="url",
        object_value="https://new.example.com",
        confidence=0.8,
    )
    _force_status(db, old_claim.id, "confirmed", "2026-01-01T00:00:00+00:00")
    _force_status(db, new_claim.id, "confirmed", "2026-01-02T00:00:00+00:00")
    (workspace / "README.md").write_text("Current endpoint https://new.example.com\n", encoding="utf-8")

    run_steward(
        service,
        mode="manual",
        max_cycles=1,
        max_claims=10,
        max_proposals=10,
        max_probe_files=20,
        probe_timeout_seconds=1.0,
        probe_failure_threshold=2,
        apply=False,
        artifact_path=workspace / "artifacts" / "steward_parity_report.json",
    )
    proposals = list_steward_proposals(service, limit=20, include_resolved=False)
    assert proposals
    return int(proposals[0]["proposal_event_id"])


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        status = int(response.status)
        parsed = json.loads(response.read().decode("utf-8"))
    return status, parsed


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


def test_proposal_resolution_cli_parity() -> None:
    db = _case_db("sqlite-steward-parity-cli")
    workspace = _case_workspace("steward-parity-cli")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    proposal_event_id = _seed_proposal(service, db, workspace)

    cmd_list = [
        sys.executable,
        "-m",
        "memorymaster",
        "--db",
        str(db),
        "--workspace",
        str(workspace),
        "steward-proposals",
        "--limit",
        "20",
    ]
    proc_list = subprocess.run(cmd_list, capture_output=True, text=True, check=False, timeout=30)
    assert proc_list.returncode == 0, proc_list.stderr
    listed = json.loads(proc_list.stdout)
    assert int(listed["rows"]) >= 1

    cmd_resolve = [
        sys.executable,
        "-m",
        "memorymaster",
        "--db",
        str(db),
        "--workspace",
        str(workspace),
        "resolve-proposal",
        "--action",
        "approve",
        "--proposal-event-id",
        str(proposal_event_id),
    ]
    proc_resolve = subprocess.run(cmd_resolve, capture_output=True, text=True, check=False, timeout=30)
    assert proc_resolve.returncode == 0, proc_resolve.stderr
    resolved = json.loads(proc_resolve.stdout)
    assert resolved["resolved"] is True
    assert resolved["status"] == "approved"


def test_proposal_resolution_dashboard_parity() -> None:
    db = _case_db("sqlite-steward-parity-dashboard")
    workspace = _case_workspace("steward-parity-dashboard")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    _seed_proposal(service, db, workspace)
    proposals = list_steward_proposals(service, limit=20, include_resolved=False)
    assert proposals
    target_claim_id = int(proposals[0]["claim_id"])

    operator_log = workspace / "operator_events.jsonl"
    operator_log.write_text("", encoding="utf-8")
    with _running_server(service, operator_log) as base_url:
        status, payload = _post_json(
            f"{base_url}/api/triage/action",
            {"claim_id": target_claim_id, "action": "approve_proposal"},
        )
        assert status == 200
        assert payload["ok"] is True
        assert payload["action"] == "approve_proposal"
        result = payload["result"]
        assert result["resolved"] is True
        assert result["status"] == "approved"


def test_proposal_resolution_mcp_parity() -> None:
    db = _case_db("sqlite-steward-parity-mcp")
    workspace = _case_workspace("steward-parity-mcp")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    _seed_proposal(service, db, workspace)

    from memorymaster import mcp_server

    if not hasattr(mcp_server, "list_steward_proposals") or not hasattr(mcp_server, "resolve_steward_proposal"):
        pytest.skip("MCP extra not installed; stewardship MCP tools unavailable in this environment.")

    listed = mcp_server.list_steward_proposals(db=str(db), workspace=str(workspace), limit=20, include_resolved=False)
    assert listed["ok"] is True
    assert int(listed["rows"]) >= 1
    proposal_event_id = int(listed["proposals"][0]["proposal_event_id"])

    resolved = mcp_server.resolve_steward_proposal(
        action="reject",
        db=str(db),
        workspace=str(workspace),
        proposal_event_id=proposal_event_id,
        apply_on_approve=True,
    )
    assert resolved["ok"] is True
    assert resolved["result"]["resolved"] is True
    assert resolved["result"]["status"] == "rejected"

