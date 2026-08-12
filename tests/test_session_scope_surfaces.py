from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

import memorymaster.core.access_control as access_control
import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.service import MemoryService
from memorymaster.surfaces.cli import main
from memorymaster.surfaces.dashboard import DashboardRequestHandler
from memorymaster.surfaces.session_scope import session_scope_payload


def _base(db: Path, workspace: Path) -> list[str]:
    return ["--json", "--db", str(db), "--workspace", str(workspace)]


def test_session_scope_cli_bind_show_clear(tmp_path: Path, capsys) -> None:
    db = tmp_path / "scope.db"
    workspace = tmp_path / "alpha"
    workspace.mkdir()
    assert main(
        [
            *_base(db, workspace),
            "session-scope",
            "bind",
            "--session-id",
            "cli-session",
            "--scope",
            "project:alpha",
            "--source-agent",
            "codex-session",
            "--platform",
            "codex",
        ]
    ) == 0
    bound = json.loads(capsys.readouterr().out)
    assert bound["scope"] == "project:alpha"
    assert "cli-session" not in json.dumps(bound)

    assert main(
        [*_base(db, workspace), "session-scope", "show", "--session-id", "cli-session"]
    ) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["items"][0]["scope"] == "project:alpha"

    assert main(
        [
            *_base(db, workspace),
            "session-scope",
            "clear",
            "--session-id",
            "cli-session",
            "--source-agent",
            "codex-session",
        ]
    ) == 0
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["ended"] == 1


@pytest.fixture
def local_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    access_control._agent_roles.clear()
    monkeypatch.setattr(access_control, "_loaded", True)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    workspace = tmp_path / "alpha"
    workspace.mkdir()
    yield tmp_path / "mcp.db", workspace
    access_control._agent_roles.clear()


def test_session_scope_mcp_round_trip(local_mcp) -> None:
    db, workspace = local_mcp
    bound = mcp_server.session_scope_bind(
        session_id="mcp-session",
        scope="project:alpha",
        source_agent="hermes-vm",
        platform="hermes",
        db=str(db),
        workspace=str(workspace),
    )
    assert bound["ok"] is True
    assert bound["scope"] == "project:alpha"
    shown = mcp_server.session_scope_show(
        session_id="mcp-session",
        source_agent="hermes-vm",
        db=str(db),
        workspace=str(workspace),
    )
    assert shown["items"][0]["session_hash"] == bound["session_hash"]
    cleared = mcp_server.session_scope_clear(
        session_id="mcp-session",
        source_agent="hermes-vm",
        db=str(db),
        workspace=str(workspace),
    )
    assert cleared["ended"] == 1


def test_team_scope_binding_rejects_global_before_database_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "alpha"
    workspace.mkdir()
    db = tmp_path / "team.db"
    access_control._agent_roles.clear()
    access_control.set_role("writer", access_control.Role.WRITER)
    monkeypatch.setattr(access_control, "_loaded", True)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "team")
    monkeypatch.setenv("MEMORYMASTER_MCP_PRINCIPAL", "writer")
    monkeypatch.setenv("MEMORYMASTER_MCP_TENANT_ID", "tenant")
    monkeypatch.setenv("MEMORYMASTER_MCP_WORKSPACE", str(workspace))
    monkeypatch.setenv("MEMORYMASTER_MCP_ALLOWED_SCOPES", "project:alpha")
    monkeypatch.setenv("MEMORYMASTER_MCP_DB", str(db))
    with pytest.raises(PermissionError, match="outside the authenticated context"):
        mcp_server.session_scope_bind(
            session_id="team-session",
            scope="global",
            db=str(db),
            workspace=str(workspace),
        )
    assert not db.exists()
    access_control._agent_roles.clear()


def test_session_scope_dashboard_payload_is_bounded_and_redacted(tmp_path: Path) -> None:
    service = MemoryService(tmp_path / "scope.db", workspace_root=tmp_path)
    service.init_db()
    from memorymaster.core.session_scope import SessionScopeRepository

    SessionScopeRepository(service.store.db_path).bind(
        "dashboard-session",
        scope="project:alpha",
        source_agent="hermes-vm",
        platform="hermes",
        binding_source="explicit",
    )
    payload = session_scope_payload(service, limit=10)
    assert payload["ok"] is True
    assert payload["rows"] == 1
    assert payload["items"][0]["scope"] == "project:alpha"
    assert "dashboard-session" not in json.dumps(payload)


def test_dashboard_renders_active_session_scope_panel() -> None:
    handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
    handler.wfile = BytesIO()
    handler.send_response = lambda *_args, **_kwargs: None
    handler.send_header = lambda *_args, **_kwargs: None
    handler.end_headers = lambda *_args, **_kwargs: None
    handler._write_dashboard()
    html = handler.wfile.getvalue().decode("utf-8")
    assert 'id="scope-bindings-body"' in html
    assert "/api/session-bindings?limit=100" in html
