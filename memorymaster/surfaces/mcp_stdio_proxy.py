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
    MEMORYMASTER_PROXY_ROOTS       the server's workspace allowlist; a session outside it runs
                                   the local stdio server instead of failing every call
    MEMORYMASTER_PROXY_CACHE       file with the server's last initialize/list answers; default
                                   %LOCALAPPDATA%/MemoryMaster/relay-listings.json
Standard library only; keep it that way so each relay stays a few MB.

Server boot (56-144 s measured after logon, the ML pre-import) is longer than a
client's MCP connect timeout (Claude Code: 30 s). While the server refuses
connections the relay answers initialize and the list requests from the last
answers it saw, so the client connects; tool calls wait for the server.
"""
from __future__ import annotations

import contextlib
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

from memorymaster.core.shared_mcp import DEFAULT_URL, SHARED_KEY, TOKEN_ENV, resolve_token  # noqa: F401 - re-exported

WORKSPACE_HEADER = "X-MM-Workspace"
TIMEOUT_SECONDS = 120
CONNECT_RETRY_SECONDS = 180  # longest server boot measured 2026-09-30..10-01: 144 s
CACHE_ENV = "MEMORYMASTER_PROXY_CACHE"
# Answers that describe the server rather than the caller's data: safe to replay while it boots.
CACHEABLE_METHODS = frozenset({
    "initialize", "tools/list", "prompts/list", "resources/list", "resources/templates/list",
})

_stdout_lock = threading.Lock()


class ListingCache:
    """The server's last initialize/list results per URL, kept in one JSON file.

    Holds tool and prompt schemas only, never call results. Writes go through a
    temp file and os.replace so concurrent relays never read a torn file.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, url: str, method: str) -> dict | None:
        result = self._read().get(url, {}).get(method)
        return result if isinstance(result, dict) else None

    def put(self, url: str, method: str, result: dict) -> None:
        with self._lock:
            data = self._read()
            if data.get(url, {}).get(method) == result:
                return
            data.setdefault(url, {})[method] = result
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(data, handle)
                os.replace(tmp, self.path)
            except OSError:
                with contextlib.suppress(OSError):
                    os.remove(tmp)


def default_cache_path(environ=os.environ) -> str:
    configured = (environ.get(CACHE_ENV) or "").strip()
    if configured:
        return configured
    base = environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".memorymaster")
    return os.path.join(base, "MemoryMaster", "relay-listings.json")


def _cache_key(message: dict) -> str | None:
    """The method when its answer is replayable: a first page only, never a cursor page."""
    method = message.get("method")
    params = message.get("params") or {}
    if method in CACHEABLE_METHODS and "id" in message and not (isinstance(params, dict) and params.get("cursor")):
        return method
    return None


def resolve_workspace(environ=os.environ) -> str:
    for name in ("MEMORYMASTER_PROXY_WORKSPACE", "CLAUDE_PROJECT_DIR"):
        value = (environ.get(name) or "").strip()
        if value:
            return os.path.abspath(value)
    return os.getcwd()


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
    cache: ListingCache | None = None,
) -> list[dict]:
    message = json.loads(raw)
    key = _cache_key(message) if cache and isinstance(message, dict) else None
    deadline = time.monotonic() + connect_retry_seconds
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
                replies = _parse_body(response.read(), response.headers.get("Content-Type", ""))
            if key:
                for reply in replies:
                    if reply.get("id") == message["id"] and isinstance(reply.get("result"), dict):
                        cache.put(url, key, reply["result"])
            return replies
        except urllib.error.HTTPError as exc:
            reply = _error_for(message, f"memorymaster shared MCP returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            cached = cache.get(url, key) if key and _refused(exc) else None
            if cached is not None:
                # Server booting or restarting: describe it from its last answer so the
                # client's connect timeout does not expire; tool calls still wait for it.
                return [{"jsonrpc": "2.0", "id": message["id"], "result": cached}]
            if _refused(exc) and time.monotonic() < deadline:
                # Nothing reached the server (not up yet at logon, or restarting): safe to resend.
                # The budget is wall-clock: a refused connect itself takes ~2 s on Windows.
                sleep(1.0)
                continue
            if _timed_out(exc):
                text = (f"memorymaster shared MCP at {url} timed out after {TIMEOUT_SECONDS} s; "
                        "the server may still be executing the call")
            else:
                text = f"memorymaster shared MCP unreachable at {url}: {type(exc).__name__}"
            reply = _error_for(message, text)
        return [reply] if reply else []


def outside_served_roots(workspace: str, environ=os.environ) -> bool:
    """True when MEMORYMASTER_PROXY_ROOTS is set and the workspace is not under it.

    Same comma-separated exact-or-glob form as the server's workspace allowlist:
    such a session would get a path-policy error for every tool from the shared
    server, so it runs today's local stdio server instead.
    """
    raw = (environ.get("MEMORYMASTER_PROXY_ROOTS") or "").strip()
    if not raw:
        return False
    import fnmatch

    path = os.path.normcase(os.path.abspath(workspace))
    for entry in (item.strip() for item in raw.split(",")):
        if not entry:
            continue
        pattern = os.path.normcase(os.path.abspath(entry)) if "*" not in entry else os.path.normcase(entry)
        if path == pattern or fnmatch.fnmatch(path, pattern):
            return False
    return True


def _run_local_server() -> int:
    import subprocess

    return subprocess.call([sys.executable, "-I", "-m", "memorymaster.mcp_server"])


def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")  # MCP stdio is UTF-8; Windows pipes default to cp1252
    workspace = resolve_workspace()
    if outside_served_roots(workspace):
        return _run_local_server()  # inherits this relay's stdin/stdout: the client talks to it directly
    url = os.environ.get("MEMORYMASTER_SHARED_MCP_URL", DEFAULT_URL).strip() or DEFAULT_URL
    token = resolve_token()
    if not token:
        sys.stderr.write(f"{TOKEN_ENV} is required for the shared MCP relay (env or HKCU\\{SHARED_KEY})\n")
        return 2

    cache = ListingCache(default_cache_path())

    def handle(raw: str) -> None:
        try:
            for message in forward(raw, url=url, token=token, workspace=workspace, cache=cache):
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
