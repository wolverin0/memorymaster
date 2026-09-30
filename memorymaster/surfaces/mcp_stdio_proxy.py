"""Thin stdio -> shared streamable-HTTP relay for the MemoryMaster MCP server (T-0726).

Every MCP client already launches a stdio command from the project directory.
This relay keeps that contract but loads no ML stack: it forwards each JSON-RPC
line to one shared HTTP server and tags it with the client's workspace, so the
server resolves the project scope per request instead of from its own cwd.

Configuration (environment):
    MEMORYMASTER_SHARED_MCP_URL    default http://127.0.0.1:8766/mcp
    MEMORYMASTER_MCP_HTTP_TOKEN    bearer token of the shared server (required; on Windows
                                   falls back to the HKCU key SHARED_KEY below)
    MEMORYMASTER_PROXY_WORKSPACE   overrides the workspace; else CLAUDE_PROJECT_DIR, else cwd
Standard library only; keep it that way so each relay stays a few MB.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_URL = "http://127.0.0.1:8766/mcp"
WORKSPACE_HEADER = "X-MM-Workspace"
TIMEOUT_SECONDS = 120
CONNECT_RETRY_SECONDS = 60
TOKEN_ENV = "MEMORYMASTER_MCP_HTTP_TOKEN"
SHARED_KEY = r"Software\MemoryMaster\SharedMcp"

_stdout_lock = threading.Lock()


def resolve_workspace(environ=os.environ) -> str:
    for name in ("MEMORYMASTER_PROXY_WORKSPACE", "CLAUDE_PROJECT_DIR"):
        value = (environ.get(name) or "").strip()
        if value:
            return os.path.abspath(value)
    return os.getcwd()


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


def _emit(message: dict) -> None:
    line = json.dumps(message, ensure_ascii=False)
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _parse_body(body: bytes, content_type: str) -> list[dict]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    if "text/event-stream" in content_type:
        return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else [parsed]


def _error_for(message: dict, text: str) -> dict | None:
    if not isinstance(message, dict) or "id" not in message or "method" not in message:
        return None  # notifications and responses get no reply
    return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": text}}


def header_workspace(workspace: str) -> str:
    """HTTP headers are Latin-1: send a non-ASCII path as RFC 8187 utf-8''<percent-encoded>."""
    if workspace.isascii():
        return workspace
    return "utf-8''" + urllib.parse.quote(workspace, safe="")


def _refused(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ConnectionRefusedError)


def _timed_out(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (TimeoutError, socket.timeout))


def forward(
    raw: str,
    *,
    url: str,
    token: str,
    workspace: str,
    opener=urllib.request.urlopen,
    connect_retry_seconds: float = CONNECT_RETRY_SECONDS,
    sleep=time.sleep,
) -> list[dict]:
    message = json.loads(raw)
    waited = 0.0
    while True:
        try:
            request = urllib.request.Request(
                url,
                data=raw.encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {token}",
                    WORKSPACE_HEADER: header_workspace(workspace),
                },
            )
            with opener(request, timeout=TIMEOUT_SECONDS) as response:
                return _parse_body(response.read(), response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            reply = _error_for(message, f"memorymaster shared MCP returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if _refused(exc) and waited < connect_retry_seconds:
                # Nothing reached the server (not up yet at logon, or restarting): safe to resend.
                sleep(1.0)
                waited += 1.0
                continue
            if _timed_out(exc):
                text = (f"memorymaster shared MCP at {url} timed out after {TIMEOUT_SECONDS} s; "
                        "the server may still be executing the call")
            else:
                text = f"memorymaster shared MCP unreachable at {url}: {type(exc).__name__}"
            reply = _error_for(message, text)
        return [reply] if reply else []


def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")  # MCP stdio is UTF-8; Windows pipes default to cp1252
    url =os.environ.get("MEMORYMASTER_SHARED_MCP_URL", DEFAULT_URL).strip() or DEFAULT_URL
    token = resolve_token()
    if not token:
        sys.stderr.write(f"{TOKEN_ENV} is required for the shared MCP relay (env or HKCU\\{SHARED_KEY})\n")
        return 2
    workspace = resolve_workspace()

    def handle(raw: str) -> None:
        try:
            for message in forward(raw, url=url, token=token, workspace=workspace):
                _emit(message)
        except ValueError:
            _emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})

    with ThreadPoolExecutor(max_workers=8) as pool:
        for line in sys.stdin:
            if line.strip():
                pool.submit(handle, line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
