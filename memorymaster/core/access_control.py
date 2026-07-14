"""Role-based access control for multi-agent memory coordination.

Three roles:
  - admin: full control (ingest, query, delete, configure)
  - writer: ingest + query (default for most agents)
  - reader: query only (dashboard, monitoring agents)

Usage:
    from memorymaster.core.access_control import check_permission, Role

    check_permission("claude-code", "ingest")  # OK (default writer)
    check_permission("dashboard", "ingest")    # raises PermissionError (reader)
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping
import contextlib

logger = logging.getLogger(__name__)

ROLES_CONFIG_PATH = os.environ.get("MEMORYMASTER_ROLES_CONFIG", "")


class Role(str, Enum):
    ADMIN = "admin"
    WRITER = "writer"
    READER = "reader"


class AuthMode(str, Enum):
    LOCAL_TRUSTED = "local-trusted"
    TEAM = "team"


@dataclass(frozen=True, slots=True)
class RequestContext:
    mode: AuthMode
    principal: str
    role: Role
    tenant_id: str | None
    workspace: str
    allowed_scopes: frozenset[str]
    allow_sensitive: bool
    db_target: str


# Permissions per role
ROLE_PERMISSIONS: dict[Role, set[str]] = {
    Role.ADMIN: {"ingest", "query", "delete", "configure", "export", "steward", "compact"},
    Role.WRITER: {"ingest", "query", "export"},
    Role.READER: {"query", "export"},
}

# Default role for unknown agents
DEFAULT_ROLE = Role.WRITER

# Agent → role mapping (loaded from config or env)
_agent_roles: dict[str, Role] = {}
_loaded = False


def _load_roles() -> None:
    """Load agent roles from config file or environment."""
    global _agent_roles, _loaded
    if _loaded:
        return
    _loaded = True

    # Try config file
    config_path = ROLES_CONFIG_PATH or ""
    if config_path and Path(config_path).exists():
        try:
            data = json.loads(Path(config_path).read_text(encoding="utf-8"))
            for agent_id, role_str in data.get("agents", {}).items():
                try:
                    _agent_roles[agent_id.lower()] = Role(role_str.lower())
                except ValueError:
                    logger.warning("Unknown role '%s' for agent '%s'", role_str, agent_id)
        except Exception as exc:
            logger.warning("Failed to load roles config: %s", exc)

    # Environment overrides: MEMORYMASTER_ROLE_<AGENT>=<role>
    for key, value in os.environ.items():
        if key.startswith("MEMORYMASTER_ROLE_"):
            agent_id = key[len("MEMORYMASTER_ROLE_"):].lower().replace("_", "-")
            with contextlib.suppress(ValueError):
                _agent_roles[agent_id] = Role(value.lower())


def get_role(agent_id: str | None) -> Role:
    """Get the role for an agent. Returns DEFAULT_ROLE if not configured or None."""
    _load_roles()
    if not agent_id or not isinstance(agent_id, str):
        logger.debug("get_role: agent_id is None or invalid, returning DEFAULT_ROLE")
        return DEFAULT_ROLE
    return _agent_roles.get(agent_id.lower(), DEFAULT_ROLE)


def get_configured_role(agent_id: str | None) -> Role | None:
    """Return only an explicitly configured role; never the legacy default."""
    _load_roles()
    if not agent_id or not isinstance(agent_id, str):
        return None
    return _agent_roles.get(agent_id.lower())


def _is_postgres_target(db_target: str, env: Mapping[str, str]) -> bool:
    target = str(db_target or "").strip().lower()
    backend = str(env.get("MEMORYMASTER_STORE_BACKEND", "")).strip().lower()
    return target.startswith(("postgres://", "postgresql://")) or backend == "postgres"


def _team_value(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, "")).strip()
    if not value:
        raise PermissionError(f"Team MCP authorization requires {name}.")
    return value


def _parse_team_scopes(raw: str) -> frozenset[str]:
    scopes = frozenset(part.strip() for part in raw.split(",") if part.strip())
    if not scopes or any("*" in scope for scope in scopes):
        raise PermissionError("Team MCP authorization requires explicit non-wildcard scopes.")
    return scopes


def _local_trusted_context(
    env: Mapping[str, str],
    db_target: str,
    workspace: str,
) -> RequestContext:
    if _is_postgres_target(db_target, env):
        raise PermissionError(
            "Local-trusted MCP mode is SQLite-only; Postgres requires team authority."
        )
    return RequestContext(
        mode=AuthMode.LOCAL_TRUSTED,
        principal=str(env.get("MEMORYMASTER_MCP_PRINCIPAL", "")).strip()
        or "mcp-session",
        role=Role.ADMIN,
        tenant_id=None,
        workspace=str(workspace or "").strip(),
        allowed_scopes=frozenset(),
        allow_sensitive=True,
        db_target=str(db_target or "").strip(),
    )


def _team_context(env: Mapping[str, str]) -> RequestContext:
    principal = _team_value(env, "MEMORYMASTER_MCP_PRINCIPAL")
    role = get_configured_role(principal)
    if role is None:
        raise PermissionError("Team MCP principal has no explicitly configured role.")
    sensitive = str(env.get("MEMORYMASTER_MCP_ALLOW_SENSITIVE", "")).strip().lower()
    if sensitive in {"1", "true", "yes", "on"}:
        raise PermissionError(
            "Sensitive reads remain disabled in team mode until database policy support exists."
        )
    return RequestContext(
        mode=AuthMode.TEAM,
        principal=principal,
        role=role,
        tenant_id=_team_value(env, "MEMORYMASTER_MCP_TENANT_ID"),
        workspace=_team_value(env, "MEMORYMASTER_MCP_WORKSPACE"),
        allowed_scopes=_parse_team_scopes(
            _team_value(env, "MEMORYMASTER_MCP_ALLOWED_SCOPES")
        ),
        allow_sensitive=False,
        db_target=_team_value(env, "MEMORYMASTER_MCP_DB"),
    )


def resolve_request_context(
    *,
    db_target: str = "",
    workspace: str = "",
    environ: Mapping[str, str] | None = None,
) -> RequestContext:
    """Derive MCP authority from operator configuration, never tool arguments."""
    env = os.environ if environ is None else environ
    raw_mode = str(env.get("MEMORYMASTER_MCP_AUTH_MODE", "")).strip().lower()
    if not raw_mode:
        raise PermissionError("MCP access requires an explicit authorization mode.")
    try:
        mode = AuthMode(raw_mode)
    except ValueError as exc:
        raise PermissionError("MEMORYMASTER_MCP_AUTH_MODE must be local-trusted or team.") from exc

    if mode is AuthMode.LOCAL_TRUSTED:
        return _local_trusted_context(env, db_target, workspace)
    return _team_context(env)


_request_context: ContextVar[RequestContext | None] = ContextVar(
    "memorymaster_request_context",
    default=None,
)


@contextmanager
def bind_request_context(context: RequestContext) -> Iterator[RequestContext]:
    token = _request_context.set(context)
    try:
        yield context
    finally:
        _request_context.reset(token)


def current_request_context(*, required: bool = False) -> RequestContext | None:
    context = _request_context.get()
    if required and context is None:
        raise PermissionError("No authenticated MCP request context is bound.")
    return context


def authorize_context_action(context: RequestContext, action: str) -> None:
    if action not in ROLE_PERMISSIONS.get(context.role, set()):
        raise PermissionError(
            f"MCP principal '{context.principal}' with role '{context.role.value}' "
            f"cannot perform '{action}'."
        )


def check_permission(agent_id: str | None, action: str) -> bool:
    """Check if an agent has permission for an action. Returns True/False.

    Handles None agent_id gracefully (treats as DEFAULT_ROLE).
    """
    if not action or not isinstance(action, str):
        logger.warning("check_permission: action is None or invalid")
        return False
    role = get_role(agent_id)
    return action in ROLE_PERMISSIONS.get(role, set())


def require_permission(agent_id: str | None, action: str) -> None:
    """Raise PermissionError if agent lacks permission for action.

    Error message includes action name for debugging.
    Handles None agent_id gracefully.
    """
    if not check_permission(agent_id, action):
        role = get_role(agent_id)
        agent_name = agent_id or "unknown"
        action_name = action or "unknown_action"
        raise PermissionError(
            f"Agent '{agent_name}' (role={role.value}) "
            f"does not have '{action_name}' permission. "
            f"Required permissions for {action_name}: {ROLE_PERMISSIONS.get(Role.ADMIN, set())}"
        )


def set_role(agent_id: str, role: Role) -> None:
    """Set an agent's role at runtime."""
    _load_roles()
    _agent_roles[agent_id.lower()] = role
    logger.info("Set role for '%s': %s", agent_id, role.value)


def list_agents() -> dict[str, str]:
    """Return all configured agent → role mappings."""
    _load_roles()
    return {agent: role.value for agent, role in _agent_roles.items()}
