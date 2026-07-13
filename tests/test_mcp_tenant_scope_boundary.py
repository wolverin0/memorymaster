from __future__ import annotations

import sqlite3

import pytest

import memorymaster.core.access_control as access_control
import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


@pytest.fixture
def team_claims(tmp_path, monkeypatch):
    db = str(tmp_path / "team.db")
    workspace = tmp_path / "alpha"
    workspace.mkdir()
    alpha = MemoryService(db, workspace_root=workspace, tenant_id="tenant-alpha")
    alpha.init_db()
    alpha_claim = alpha.ingest(
        "authorization matrix marker alpha allowed",
        [CitationInput(source="test://alpha")],
        scope="project:alpha",
        source_agent="seed",
    )
    beta_scope_claim = alpha.ingest(
        "authorization matrix marker beta scope forbidden",
        [CitationInput(source="test://beta-scope")],
        scope="project:beta",
        source_agent="seed",
    )
    beta_tenant = MemoryService(db, workspace_root=workspace, tenant_id="tenant-beta")
    beta_tenant_claim = beta_tenant.ingest(
        "authorization matrix marker beta tenant forbidden",
        [CitationInput(source="test://beta-tenant")],
        scope="project:alpha",
        source_agent="seed",
    )

    access_control._agent_roles.clear()
    monkeypatch.setattr(access_control, "_loaded", True)
    access_control.set_role("mcp-reader", access_control.Role.READER)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "team")
    monkeypatch.setenv("MEMORYMASTER_MCP_PRINCIPAL", "mcp-reader")
    monkeypatch.setenv("MEMORYMASTER_MCP_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("MEMORYMASTER_MCP_WORKSPACE", str(workspace))
    monkeypatch.setenv("MEMORYMASTER_MCP_ALLOWED_SCOPES", "project:alpha,global")
    monkeypatch.setenv("MEMORYMASTER_MCP_DB", db)
    yield db, workspace, alpha_claim, beta_scope_claim, beta_tenant_claim
    access_control._agent_roles.clear()


def test_team_list_and_query_enforce_tenant_and_scope(team_claims) -> None:
    db, _workspace, allowed, wrong_scope, wrong_tenant = team_claims

    listed = mcp_server.list_claims(limit=20)
    queried = mcp_server.query_memory(
        query="authorization matrix marker",
        retrieval_mode="legacy",
        scope_allowlist="project:alpha",
        limit=20,
    )

    assert {claim["id"] for claim in listed["claims"]} == {allowed.id}
    assert {claim["id"] for claim in queried["claims"]} == {allowed.id}
    with sqlite3.connect(db) as conn:
        counts = dict(conn.execute("SELECT id, access_count FROM claims"))
    assert counts[wrong_scope.id] == 0
    assert counts[wrong_tenant.id] == 0


def test_team_context_rejects_caller_db_and_workspace_switch(team_claims, tmp_path) -> None:
    with pytest.raises(PermissionError, match="database"):
        mcp_server.list_claims(db=str(tmp_path / "other.db"))
    with pytest.raises(PermissionError, match="workspace"):
        mcp_server.list_claims(workspace=str(tmp_path / "other-workspace"))


def test_team_ingest_uses_authenticated_principal_and_tenant(team_claims) -> None:
    db, _workspace, *_claims = team_claims
    access_control.set_role("mcp-reader", access_control.Role.WRITER)

    result = mcp_server.ingest_claim(
        text="authenticated team writer marker",
        sources_json='["test://writer"]',
        source_agent="forged-writer",
        scope="project:alpha",
    )

    with sqlite3.connect(db) as conn:
        stored = conn.execute(
            "SELECT tenant_id, scope, source_agent FROM claims WHERE id = ?",
            (result["claim"]["id"],),
        ).fetchone()
    assert stored == ("tenant-alpha", "project:alpha", "mcp-reader")
