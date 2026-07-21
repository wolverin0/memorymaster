from __future__ import annotations

import asyncio

import pytest

import memorymaster.core.access_control as access_control
import memorymaster.surfaces.mcp_server as mcp_server


EXPECTED_TOOLS = {
    "archive_by_source",
    "checkpoint",
    "classify_query",
    "compact_memory",
    "dream_status",
    "entity_stats",
    "extract_entities",
    "federated_query",
    "find_related_claims",
    "get_usage_rollup",
    "ingest_claim",
    "ingest_rule",
    "init_db",
    "list_claims",
    "list_events",
    "list_steward_proposals",
    "local_search",
    "open_dashboard",
    "pin_claim",
    "quality_scores",
    "query_claim_paths",
    "query_for_context",
    "query_for_task",
    "query_memory",
    "query_meta_decisions",
    "query_rules",
    "read_active_tasks",
    "recall_analysis",
    "recompute_tiers",
    "redact_claim_payload",
    "resolve_project",
    "resolve_steward_proposal",
    "rules_export",
    "run_cycle",
    "run_steward",
    "search_verbatim",
    "volunteer_context",
}


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch, tmp_path):
    access_control._agent_roles.clear()
    monkeypatch.setattr(access_control, "_loaded", True)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "team")
    monkeypatch.setenv("MEMORYMASTER_MCP_PRINCIPAL", "mcp-reader")
    monkeypatch.setenv("MEMORYMASTER_MCP_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("MEMORYMASTER_MCP_WORKSPACE", str(tmp_path / "alpha"))
    monkeypatch.setenv("MEMORYMASTER_MCP_ALLOWED_SCOPES", "project:alpha,global")
    monkeypatch.setenv("MEMORYMASTER_MCP_DB", str(tmp_path / "team.db"))
    yield
    access_control._agent_roles.clear()


def test_every_registered_mcp_tool_has_a_named_action() -> None:
    assert set(mcp_server.MCP_TOOL_POLICIES) == EXPECTED_TOOLS
    assert all(policy.action for policy in mcp_server.MCP_TOOL_POLICIES.values())
    registered = mcp_server.mcp._tool_manager.list_tools()
    assert {tool.name for tool in registered} == EXPECTED_TOOLS
    assert all(getattr(tool.fn, "__mcp_action__", None) for tool in registered)


def test_reader_denial_happens_before_tool_body(monkeypatch) -> None:
    access_control.set_role("mcp-reader", access_control.Role.READER)
    monkeypatch.setattr(
        mcp_server,
        "_service",
        lambda *_args, **_kwargs: pytest.fail("denied request reached the service"),
    )

    with pytest.raises(PermissionError, match="cannot perform 'ingest'"):
        mcp_server.ingest_claim(text="reader write", sources_json='["test://reader"]')


def test_registered_tool_denies_reader_before_tool_body(monkeypatch) -> None:
    access_control.set_role("mcp-reader", access_control.Role.READER)
    monkeypatch.setattr(
        mcp_server,
        "_service",
        lambda *_args, **_kwargs: pytest.fail("registered denial reached the service"),
    )

    with pytest.raises(Exception) as exc_info:
        asyncio.run(
            mcp_server.mcp._tool_manager.call_tool(
                "ingest_claim",
                {"text": "reader write", "sources_json": '["test://reader"]'},
            )
        )

    cause = exc_info.value.__cause__
    assert isinstance(cause, PermissionError)
    assert "cannot perform 'ingest'" in str(cause)


def test_missing_team_identity_fails_before_tool_body(monkeypatch) -> None:
    monkeypatch.delenv("MEMORYMASTER_MCP_PRINCIPAL")
    monkeypatch.setattr(
        mcp_server,
        "_service",
        lambda *_args, **_kwargs: pytest.fail("unauthenticated request reached the service"),
    )

    with pytest.raises(PermissionError, match="MEMORYMASTER_MCP_PRINCIPAL"):
        mcp_server.query_memory(query="must fail before opening the database")


@pytest.mark.parametrize(
    "tool_name",
    sorted(name for name, policy in mcp_server.MCP_TOOL_POLICIES.items() if not policy.team_enabled),
)
def test_unverified_team_tools_fail_before_body(monkeypatch, tool_name: str) -> None:
    access_control.set_role("mcp-reader", access_control.Role.ADMIN)

    def sentinel() -> None:
        pytest.fail(f"disabled team tool reached body: {tool_name}")

    sentinel.__name__ = tool_name
    guarded = mcp_server._authorized_tool_callable(
        sentinel,
        mcp_server.MCP_TOOL_POLICIES[tool_name],
    )
    with pytest.raises(PermissionError, match="disabled in team mode"):
        guarded()
