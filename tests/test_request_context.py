from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

import memorymaster.core.access_control as access_control


@pytest.fixture(autouse=True)
def isolated_roles(monkeypatch):
    access_control._agent_roles.clear()
    monkeypatch.setattr(access_control, "_loaded", True)
    yield
    access_control._agent_roles.clear()


def _team_env(**overrides: str) -> dict[str, str]:
    values = {
        "MEMORYMASTER_MCP_AUTH_MODE": "team",
        "MEMORYMASTER_MCP_PRINCIPAL": "mcp-reader",
        "MEMORYMASTER_MCP_TENANT_ID": "tenant-alpha",
        "MEMORYMASTER_MCP_WORKSPACE": "C:/work/alpha",
        "MEMORYMASTER_MCP_ALLOWED_SCOPES": "project:alpha,global",
        "MEMORYMASTER_MCP_DB": "postgresql://memorymaster.invalid/app",
    }
    values.update(overrides)
    return values


def test_local_context_is_explicit_trusted_and_immutable() -> None:
    context = access_control.resolve_request_context(
        db_target="memorymaster.db",
        workspace="C:/work/alpha",
        environ={},
    )

    assert context.mode is access_control.AuthMode.LOCAL_TRUSTED
    assert context.role is access_control.Role.ADMIN
    assert context.principal == "mcp-session"
    with pytest.raises(FrozenInstanceError):
        context.principal = "forged"  # type: ignore[misc]


def test_context_binding_is_scoped_and_reset() -> None:
    context = access_control.resolve_request_context(environ={})

    assert access_control.current_request_context() is None
    with access_control.bind_request_context(context):
        assert access_control.current_request_context(required=True) is context
    assert access_control.current_request_context() is None


@pytest.mark.parametrize(
    "missing",
    [
        "MEMORYMASTER_MCP_PRINCIPAL",
        "MEMORYMASTER_MCP_TENANT_ID",
        "MEMORYMASTER_MCP_WORKSPACE",
        "MEMORYMASTER_MCP_ALLOWED_SCOPES",
        "MEMORYMASTER_MCP_DB",
    ],
)
def test_team_context_requires_complete_operator_configuration(missing: str) -> None:
    access_control.set_role("mcp-reader", access_control.Role.READER)
    env = _team_env()
    del env[missing]

    with pytest.raises(PermissionError, match=missing):
        access_control.resolve_request_context(environ=env)


def test_team_context_requires_an_explicit_role() -> None:
    with pytest.raises(PermissionError, match="explicitly configured role"):
        access_control.resolve_request_context(environ=_team_env())


def test_team_context_carries_frozen_authority() -> None:
    access_control.set_role("mcp-reader", access_control.Role.READER)
    context = access_control.resolve_request_context(environ=_team_env())

    assert context.mode is access_control.AuthMode.TEAM
    assert context.tenant_id == "tenant-alpha"
    assert context.allowed_scopes == ("project:alpha", "global")
    assert context.allow_sensitive is False
    access_control.authorize_context_action(context, "query")
    with pytest.raises(PermissionError, match="cannot perform 'ingest'"):
        access_control.authorize_context_action(context, "ingest")


def test_implicit_postgres_context_is_rejected() -> None:
    with pytest.raises(PermissionError, match="explicit authorization mode"):
        access_control.resolve_request_context(
            db_target="postgresql://memorymaster.invalid/app",
            environ={},
        )


def test_team_context_rejects_wildcard_scope() -> None:
    access_control.set_role("mcp-reader", access_control.Role.READER)
    with pytest.raises(PermissionError, match="non-wildcard"):
        access_control.resolve_request_context(
            environ=_team_env(MEMORYMASTER_MCP_ALLOWED_SCOPES="*"),
        )
