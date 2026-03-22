"""Role-based access control for multi-agent memory coordination.

Three roles:
  - admin: full control (ingest, query, delete, configure)
  - writer: ingest + query (default for most agents)
  - reader: query only (dashboard, monitoring agents)

Usage:
    from memorymaster.access_control import check_permission, Role

    check_permission("claude-code", "ingest")  # OK (default writer)
    check_permission("dashboard", "ingest")    # raises PermissionError (reader)
"""

from __future__ import annotations

import json
import logging
import os
from enum import Enum
from pathlib import Path
import contextlib

logger = logging.getLogger(__name__)

ROLES_CONFIG_PATH = os.environ.get("MEMORYMASTER_ROLES_CONFIG", "")


class Role(str, Enum):
    ADMIN = "admin"
    WRITER = "writer"
    READER = "reader"


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
