"""Path allowlist + admin-mode bypass for MCP tool db/workspace overrides (v3.19.0-H4).

The MCP server's tools accept caller-controlled ``db`` and ``workspace``
arguments. Pre-v3.19 those overrides were unrestricted — any caller that
could reach the MCP transport could direct writes to any SQLite file the
process had permission to open, or read from any workspace root. This
module locks that surface down by allowlisting both axes via env vars and
giving operators an explicit admin-mode escape hatch.

Env vars (all opt-in):

    MEMORYMASTER_MCP_DB_ALLOWLIST          — comma-separated absolute paths
                                              or glob patterns (fnmatch).
                                              Empty/unset = no restriction
                                              (back-compat).
    MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST   — same shape, for workspace roots.
    MEMORYMASTER_MCP_ADMIN_MODE            — set to 1 to bypass both
                                              allowlists. Use sparingly;
                                              logs a WARNING for visibility.

The validators are designed to be called from the SINGLE existing path-
resolution chokepoint in ``mcp_server._resolve_db`` and ``_resolve_workspace``,
so the policy applies uniformly to every tool without touching the 14+
tool entry points individually.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class MCPPathPolicyError(PermissionError):
    """Raised when a tool's resolved db/workspace path is not allowlisted."""


# Env var names (also kept here for tests/docs to import cleanly)
ENV_DB_ALLOWLIST = "MEMORYMASTER_MCP_DB_ALLOWLIST"
ENV_WORKSPACE_ALLOWLIST = "MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST"
ENV_ADMIN_MODE = "MEMORYMASTER_MCP_ADMIN_MODE"


def admin_mode() -> bool:
    """True if the operator has explicitly opted out of allowlist enforcement."""
    raw = os.environ.get(ENV_ADMIN_MODE, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _parse_allowlist(env_var: str) -> list[str]:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _resolve_canonical(path: str) -> str:
    """Best-effort canonical form. Tolerates non-existent paths."""
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except OSError:
        return str(path)


def _matches_any(canonical_path: str, patterns: list[str]) -> bool:
    """True if canonical_path matches any allowlist entry (exact or fnmatch glob)."""
    for pattern in patterns:
        canonical_pattern = _resolve_canonical(pattern)
        if canonical_path == canonical_pattern:
            return True
        # Glob match — supports e.g. "/home/user/projects/*/memorymaster.db".
        # Apply against both the canonical-form pattern and the raw pattern
        # so users can write either path style in their env.
        if fnmatch.fnmatch(canonical_path, canonical_pattern):
            return True
        if fnmatch.fnmatch(canonical_path, pattern):
            return True
    return False


def validate_db_path(resolved_db: str, *, actor: str | None = None) -> None:
    """Enforce the DB allowlist on a resolved db path.

    Back-compat: when ``MEMORYMASTER_MCP_DB_ALLOWLIST`` is unset, returns
    silently (no restriction). Admin mode bypasses all checks but logs
    a WARNING for visibility.

    Raises ``MCPPathPolicyError`` when policy denies.
    """
    patterns = _parse_allowlist(ENV_DB_ALLOWLIST)
    if not patterns:
        return  # no policy configured — preserves pre-v3.19 behaviour
    if admin_mode():
        logger.warning(
            "mcp_path_policy: admin_mode bypass for db=%s actor=%s",
            resolved_db,
            actor or "unknown",
        )
        return

    canonical = _resolve_canonical(resolved_db)
    if not _matches_any(canonical, patterns):
        logger.warning(
            "mcp_path_policy: denied db=%s (canonical=%s) actor=%s",
            resolved_db,
            canonical,
            actor or "unknown",
        )
        raise MCPPathPolicyError(
            f"db path {resolved_db!r} (resolved to {canonical!r}) is not in "
            f"{ENV_DB_ALLOWLIST}. Set {ENV_ADMIN_MODE}=1 to bypass."
        )


def validate_workspace_path(resolved_workspace: str, *, actor: str | None = None) -> None:
    """Enforce the workspace allowlist on a resolved workspace path.

    Behaviour mirrors ``validate_db_path``: silent when unset, raises
    ``MCPPathPolicyError`` when policy denies.
    """
    patterns = _parse_allowlist(ENV_WORKSPACE_ALLOWLIST)
    if not patterns:
        return
    if admin_mode():
        logger.warning(
            "mcp_path_policy: admin_mode bypass for workspace=%s actor=%s",
            resolved_workspace,
            actor or "unknown",
        )
        return

    canonical = _resolve_canonical(resolved_workspace)
    if not _matches_any(canonical, patterns):
        logger.warning(
            "mcp_path_policy: denied workspace=%s (canonical=%s) actor=%s",
            resolved_workspace,
            canonical,
            actor or "unknown",
        )
        raise MCPPathPolicyError(
            f"workspace path {resolved_workspace!r} (resolved to {canonical!r}) "
            f"is not in {ENV_WORKSPACE_ALLOWLIST}. Set {ENV_ADMIN_MODE}=1 to bypass."
        )
