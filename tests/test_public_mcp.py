from __future__ import annotations

from pathlib import Path
import hashlib
import json

import pytest

import memorymaster.core.access_control as access_control
import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.models import CitationInput
from memorymaster.knowledge.skill_schema import build_skill_fields


@pytest.fixture
def mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    db = str(tmp_path / "mcp.db")
    workspace = str(tmp_path / "workspace")
    Path(workspace).mkdir()
    access_control._agent_roles.clear()
    monkeypatch.setattr(access_control, "_loaded", True)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_ROOTS", f"mcp={workspace}")
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_TRUST_MODE", "local-trusted")
    yield db, workspace
    access_control._agent_roles.clear()


def test_public_mcp_contract_round_trip(mcp_env) -> None:
    db, workspace = mcp_env
    remembered = mcp_server.remember(
        text="MCP public capture", db=db, workspace=workspace
    )
    assert remembered["ok"] is True
    assert remembered["api_version"] == "memorymaster.public.v1"

    recalled = mcp_server.recall(
        query="MCP public capture", db=db, workspace=workspace
    )
    assert recalled["ok"] is True
    assert recalled["trust_mode"] == "trusted"

    preview = mcp_server.forget(
        source_item_id=remembered["source_item"]["id"],
        db=db,
        workspace=workspace,
    )
    assert preview["apply"] is False
    assert preview["evidence_preserved"] is True

    improved = mcp_server.improve(db=db, workspace=workspace)
    assert improved["ok"] is True
    assert improved["api_version"] == "memorymaster.public.v1"


def _skill_payload() -> dict[str, object]:
    return {
        "schema": "personal-skill-v1",
        "slug": "verify-release",
        "title": "Verify release",
        "when_to_use": "Use when a release gate must be checked.",
        "when_not_to_use": "Do not use for routine local edits.",
        "inputs": ["release candidate"],
        "prerequisites": ["tests collected"],
        "workflow": ["Run the focused gate", "Inspect the evidence"],
        "decision_rules": ["Stop when a required gate fails"],
        "expected_output": "An evidence-backed release verdict.",
        "validation": ["Every required check has direct evidence"],
        "pitfalls": ["Treating skipped checks as passes"],
        "recovery": ["Repair the failed gate and rerun it"],
        "quality_scores": {
            "recurrence": 16,
            "reusability": 16,
            "executability": 16,
            "validation": 16,
            "safety": 16,
        },
    }


def _ingest_skill(service, *, slug: str, scope: str, marker: str):
    payload = _skill_payload()
    payload.update(
        {
            "slug": slug,
            "title": f"Verify release {slug}",
            "workflow": [marker, "Inspect the evidence"],
        }
    )
    fields = build_skill_fields(payload, supporting_claim_ids=[7, 8])
    return service.ingest(
        **fields,
        citations=[CitationInput(source="fixture", locator=slug)],
        scope=scope,
        source_agent="fixture",
    )


def _transition(service, claim, status: str):
    return service.store.apply_status_transition(
        claim,
        to_status=status,
        reason="fixture approval",
        event_type="validator",
    )


def test_public_recall_optionally_includes_only_confirmed_scoped_skills(mcp_env) -> None:
    db, workspace = mcp_env
    service = mcp_server._service(db, workspace)
    service.init_db()
    candidate = _ingest_skill(
        service,
        slug="verify-release",
        scope="project:workspace",
        marker="Run the focused gate",
    )
    wrong_scope = _ingest_skill(
        service,
        slug="wrong-scope",
        scope="project:other",
        marker="NEVER INJECT WRONG SCOPE",
    )
    stale = _ingest_skill(
        service,
        slug="stale-skill",
        scope="project:workspace",
        marker="NEVER INJECT STALE SKILL",
    )
    _transition(service, wrong_scope, "confirmed")
    stale = _transition(service, stale, "confirmed")
    _transition(service, stale, "stale")

    before = mcp_server.recall(
        query="verify release",
        scope_allowlist="project:workspace",
        include_skills=True,
        db=db,
        workspace=workspace,
    )
    assert before["skills"] == ()
    assert "Run the focused gate" not in before["output"]

    _transition(service, candidate, "confirmed")
    after = mcp_server.recall(
        query="verify release",
        scope_allowlist="project:workspace",
        include_skills=True,
        skill_limit=2,
        db=db,
        workspace=workspace,
    )

    assert len(after["skills"]) == 1
    assert after["skills"][0]["claim_id"] == candidate.id
    assert all(item["claim_id"] != candidate.id for item in after["claims"])
    assert "=== APPROVED SKILLS ===" in after["output"]
    assert "Run the focused gate" in after["output"]
    assert "NEVER INJECT WRONG SCOPE" not in after["output"]
    assert "NEVER INJECT STALE SKILL" not in after["output"]
    assert after["tokens_used"] <= after["token_budget"]


def test_team_mcp_rejects_client_local_path_before_public_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    document = workspace / "note.txt"
    document.write_text("private file", encoding="utf-8")
    access_control._agent_roles.clear()
    access_control.set_role("writer", access_control.Role.WRITER)
    monkeypatch.setattr(access_control, "_loaded", True)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "team")
    monkeypatch.setenv("MEMORYMASTER_MCP_PRINCIPAL", "writer")
    monkeypatch.setenv("MEMORYMASTER_MCP_TENANT_ID", "tenant")
    monkeypatch.setenv("MEMORYMASTER_MCP_WORKSPACE", str(workspace))
    monkeypatch.setenv("MEMORYMASTER_MCP_ALLOWED_SCOPES", "project:workspace")
    monkeypatch.setenv("MEMORYMASTER_MCP_DB", str(tmp_path / "team.db"))
    with pytest.raises(PermissionError, match="local paths"):
        mcp_server.remember(path=str(document), db=str(tmp_path / "team.db"), workspace=str(workspace))


def test_mcp_remember_preserves_sanitized_producer_replay_identity(mcp_env) -> None:
    db, workspace = mcp_env
    text = "Hermes observed a durable decision."
    result = mcp_server.remember(
        text=text,
        scope="user",
        source_agent="hermes:otacon",
        session_id="a" * 64,
        platform="telegram",
        producer="hermes",
        producer_external_id="turn:7",
        producer_content_hash=hashlib.sha256(text.encode()).hexdigest(),
        producer_session_hash="a" * 64,
        producer_turn_id="7",
        producer_metadata_json=json.dumps({"agent_identity": "otacon"}),
        db=db,
        workspace=workspace,
    )

    with mcp_server._service(db, workspace, read_only=True).store.connect() as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM source_items WHERE id = ?",
                (result["source_item"]["id"],),
            ).fetchone()[0]
        )
    assert payload["producer"] == "hermes"
    assert payload["producer_external_id_hash"] == hashlib.sha256(b"turn:7").hexdigest()
    assert payload["producer_session_hash"] == "a" * 64
    assert payload["producer_turn_id"] == "7"


def test_team_mcp_forget_preview_is_available_but_cannot_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = tmp_path / "team.db"
    access_control._agent_roles.clear()
    access_control.set_role("writer", access_control.Role.WRITER)
    monkeypatch.setattr(access_control, "_loaded", True)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "team")
    monkeypatch.setenv("MEMORYMASTER_MCP_PRINCIPAL", "writer")
    monkeypatch.setenv("MEMORYMASTER_MCP_TENANT_ID", "tenant")
    monkeypatch.setenv("MEMORYMASTER_MCP_WORKSPACE", str(workspace))
    monkeypatch.setenv("MEMORYMASTER_MCP_ALLOWED_SCOPES", "project:workspace")
    monkeypatch.setenv("MEMORYMASTER_MCP_DB", str(db))
    remembered = mcp_server.remember(
        text="Preview-only retirement fixture.",
        scope="project:workspace",
        db=str(db),
        workspace=str(workspace),
    )

    preview = mcp_server.forget_preview(
        source_item_id=remembered["source_item"]["id"],
        db=str(db),
        workspace=str(workspace),
    )

    assert preview["apply"] is False
    assert preview["evidence_preserved"] is True


def test_team_writer_can_queue_improvement_without_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = tmp_path / "team.db"
    access_control._agent_roles.clear()
    access_control.set_role("writer", access_control.Role.WRITER)
    monkeypatch.setattr(access_control, "_loaded", True)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "team")
    monkeypatch.setenv("MEMORYMASTER_MCP_PRINCIPAL", "writer")
    monkeypatch.setenv("MEMORYMASTER_MCP_TENANT_ID", "tenant")
    monkeypatch.setenv("MEMORYMASTER_MCP_WORKSPACE", str(workspace))
    monkeypatch.setenv("MEMORYMASTER_MCP_ALLOWED_SCOPES", "project:workspace")
    monkeypatch.setenv("MEMORYMASTER_MCP_DB", str(db))
    remembered = mcp_server.remember(
        text="Queue-only improvement fixture.",
        scope="project:workspace",
        db=str(db),
        workspace=str(workspace),
    )

    result = mcp_server.improve(
        scope="project:workspace",
        max_items=1,
        db=str(db),
        workspace=str(workspace),
    )

    assert result["queued"]["extract_claims"] in {0, 1}
    assert remembered["source_item"]["id"] > 0
    with mcp_server._service(str(db), str(workspace), read_only=True).store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
