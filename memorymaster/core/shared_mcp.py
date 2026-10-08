"""Where the shared MCP server lives and how a client finds its token.

Lives in ``core`` so recall (the prompt hook) and the stdio relay share it
without a core layer importing a user-facing surface.
"""
from __future__ import annotations

import os

DEFAULT_URL = "http://127.0.0.1:8766/mcp"
TOKEN_ENV = "MEMORYMASTER_MCP_HTTP_TOKEN"
SHARED_KEY = r"Software\MemoryMaster\SharedMcp"


def resolve_token(environ=os.environ) -> str:
    """Bearer token: the environment, else the shared service's own registry key.

    The registry fallback lets clients launched before the service existed
    (their inherited environment predates it) find the token without it ever
    being written into a client config file.
    """
    token = (environ.get(TOKEN_ENV) or "").strip()
    if token or os.name != "nt":
        return token
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SHARED_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, TOKEN_ENV)
    except OSError:
        return ""
    return value.strip() if isinstance(value, str) else ""
