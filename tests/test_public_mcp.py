from __future__ import annotations

from pathlib import Path

import pytest

import memorymaster.core.access_control as access_control
import memorymaster.surfaces.mcp_server as mcp_server


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
