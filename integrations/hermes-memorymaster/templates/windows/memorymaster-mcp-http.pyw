"""Consoleless Windows launcher for the authenticated MemoryMaster MCP service."""

from __future__ import annotations

import os
import sys
from pathlib import Path

USER_ENVIRONMENT_KEYS = (
    "MEMORYMASTER_MCP_HTTP_TOKEN",
    "MEMORYMASTER_MCP_AUTH_MODE",
    "MEMORYMASTER_MCP_PRINCIPAL",
    "MEMORYMASTER_ROLE_HERMES_MEMORYMASTER",
    "MEMORYMASTER_MCP_TENANT_ID",
    "MEMORYMASTER_MCP_WORKSPACE",
    "MEMORYMASTER_MCP_ALLOWED_SCOPES",
    "MEMORYMASTER_MCP_DB",
    "MEMORYMASTER_MCP_HTTP_ALLOWED_HOSTS",
    "MEMORYMASTER_DEFAULT_DB",
    "MEMORYMASTER_WORKSPACE",
    "MEMORYMASTER_LOG_DIR",
)


def _load_user_environment() -> None:
    """Load only the allowlisted HKCU environment when Task Scheduler omitted it."""
    if os.name != "nt":
        return
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
    except OSError:
        return
    with key:
        for name in USER_ENVIRONMENT_KEYS:
            if os.environ.get(name):
                continue
            try:
                value, _kind = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                os.environ[name] = value.strip()


def _log_stream():
    configured = os.environ.get("MEMORYMASTER_LOG_DIR", "").strip()
    base = Path(configured) if configured else Path(os.environ["LOCALAPPDATA"]) / "MemoryMaster" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return (base / "mcp-http.log").open("a", encoding="utf-8", buffering=1)


if __name__ == "__main__":
    _load_user_environment()
    stream = _log_stream()
    sys.stdout = stream
    sys.stderr = stream
    # pythonw starts with no stderr: logging handlers must see the real stream.
    from memorymaster.surfaces.mcp_http import main

    raise SystemExit(main())
