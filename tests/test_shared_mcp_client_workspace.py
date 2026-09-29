"""One shared HTTP MemoryMaster serves many repositories (T-0726).

The server's own cwd must never decide a client's project scope: a client declares
its workspace with the X-MM-Workspace header (the stdio relay sends its cwd), and
that header only fills in a tool's default workspace.
"""
from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

from memorymaster.surfaces import mcp_server
from memorymaster.surfaces import mcp_stdio_proxy as relay


def _probe(db: str = "memorymaster.db", workspace: str = ".") -> str:
    return workspace


def _guarded():
    return mcp_server._authorized_tool_callable(_probe, mcp_server.McpToolPolicy("query", team_enabled=True))


def test_declared_workspace_replaces_the_default_workspace(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    monkeypatch.setattr(mcp_server, "_client_workspace_header", lambda: "G:/repos/alpha")
    assert _guarded()() == "G:/repos/alpha"


def test_explicit_workspace_argument_wins_over_the_header(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    monkeypatch.setattr(mcp_server, "_client_workspace_header", lambda: "G:/repos/alpha")
    assert _guarded()(workspace="G:/repos/explicit") == "G:/repos/explicit"


def test_without_a_header_the_default_is_unchanged(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    monkeypatch.setattr(mcp_server, "_client_workspace_header", lambda: "")
    assert _guarded()() == "."


def test_header_is_read_from_the_current_mcp_request():
    from mcp.server.lowlevel.server import request_ctx

    assert mcp_server._client_workspace_header() == ""  # stdio / no request bound
    fake = SimpleNamespace(request=SimpleNamespace(headers={"x-mm-workspace": " G:/repos/beta "}))
    token = request_ctx.set(fake)
    try:
        assert mcp_server._client_workspace_header() == "G:/repos/beta"
    finally:
        request_ctx.reset(token)


class _Response(io.BytesIO):
    def __init__(self, body: bytes, content_type: str = "application/json"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_relay_forwards_with_workspace_header_and_bearer():
    seen = {}

    def opener(request, timeout):
        seen.update({k.lower(): v for k, v in request.header_items()})
        return _Response(json.dumps({"jsonrpc": "2.0", "id": 7, "result": {}}).encode())

    raw = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    out = relay.forward(raw, url="http://127.0.0.1:1/mcp", token="t", workspace="G:/repos/alpha", opener=opener)
    assert out == [{"jsonrpc": "2.0", "id": 7, "result": {}}]
    assert seen["x-mm-workspace"] == "G:/repos/alpha"
    assert seen["authorization"] == "Bearer t"


def test_relay_notification_gets_no_reply():
    raw = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    out = relay.forward(raw, url="http://127.0.0.1:1/mcp", token="t", workspace="w", opener=lambda r, timeout: _Response(b""))
    assert out == []


def test_relay_reports_an_unreachable_server_as_a_jsonrpc_error():
    def down(request, timeout):
        raise urllib.error.URLError("refused")

    request = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call"})
    notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    [reply] = relay.forward(request, url="http://127.0.0.1:1/mcp", token="t", workspace="w", opener=down)
    assert reply["id"] == 3 and reply["error"]["code"] == -32603
    assert relay.forward(notification, url="http://127.0.0.1:1/mcp", token="t", workspace="w", opener=down) == []


def test_relay_workspace_priority(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert relay.resolve_workspace({}) == str(tmp_path)
    assert relay.resolve_workspace({"CLAUDE_PROJECT_DIR": str(tmp_path / "p")}) == str(tmp_path / "p")
    both = {"CLAUDE_PROJECT_DIR": str(tmp_path / "p"), "MEMORYMASTER_PROXY_WORKSPACE": str(tmp_path / "o")}
    assert relay.resolve_workspace(both) == str(tmp_path / "o")


def test_relay_bad_url_setting_becomes_an_error_reply_not_a_parse_error():
    request = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    [reply] = relay.forward(request, url="not a url", token="t", workspace="w")
    assert reply["id"] == 9 and "unreachable" in reply["error"]["message"]
