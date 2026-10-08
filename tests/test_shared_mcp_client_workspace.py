"""One shared HTTP MemoryMaster serves many repositories (T-0726).

The server's own cwd must never decide a client's project scope: a client declares
its workspace with the X-MM-Workspace header (the stdio relay sends its cwd), and
that header only fills in a tool's default workspace.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from types import SimpleNamespace

import pytest

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


def _over_http(headers: dict[str, str]):
    from mcp.server.lowlevel.server import request_ctx

    return request_ctx.set(SimpleNamespace(request=SimpleNamespace(headers=headers)))


def _recording_tool(action: str):
    calls: list[str] = []

    def tool(db: str = "memorymaster.db", workspace: str = ".") -> str:
        calls.append(workspace)
        return workspace

    return mcp_server._authorized_tool_callable(tool, mcp_server.McpToolPolicy(action, team_enabled=True)), calls


@pytest.mark.parametrize("action", ["ingest", "query"])
def test_shared_server_without_a_declared_workspace_fails_closed(monkeypatch, action):
    # Verifier gap (a): the "." default used to resolve to the server's cwd (project:serverdir).
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    tool, calls = _recording_tool(action)
    token = _over_http({})
    try:
        with pytest.raises(PermissionError, match="declare an absolute client workspace"):
            tool()
        with pytest.raises(PermissionError, match="declare an absolute client workspace"):
            tool(workspace="relative/repo")
    finally:
        from mcp.server.lowlevel.server import request_ctx

        request_ctx.reset(token)
    assert calls == []


def test_shared_server_accepts_a_declared_absolute_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    tool, calls = _recording_tool("ingest")
    token = _over_http({"x-mm-workspace": str(tmp_path)})
    try:
        assert tool() == str(tmp_path)
    finally:
        from mcp.server.lowlevel.server import request_ctx

        request_ctx.reset(token)
    assert calls == [str(tmp_path)]


def test_stdio_default_workspace_still_works(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    tool, calls = _recording_tool("ingest")
    assert tool() == "."
    assert calls == ["."]


@pytest.mark.parametrize(
    "placeholder",
    ["${CLAUDE_PROJECT_DIR}", "{env:PWD}", "$PWD", "%CD%", "G:/x/${workspaceFolder}"],
)
@pytest.mark.parametrize("via_header", [True, False])
def test_unexpanded_config_placeholders_are_rejected(monkeypatch, placeholder, via_header):
    # Verifier gap (b): these used to create project:claude_project_dir / project:env-pwd.
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    tool, calls = _recording_tool("ingest")
    if via_header:
        token = _over_http({"x-mm-workspace": placeholder})
        try:
            with pytest.raises(PermissionError, match="unexpanded config variable"):
                tool()
        finally:
            from mcp.server.lowlevel.server import request_ctx

            request_ctx.reset(token)
    else:  # stdio clients with a broken config are refused too
        with pytest.raises(PermissionError, match="unexpanded config variable"):
            tool(workspace=placeholder)
    assert calls == []


def test_embedding_model_loads_once_per_process(monkeypatch):
    # Verifier gap (c): the stateless server builds a service per request and reloaded the model (~2.7 s).
    from memorymaster.recall import embeddings

    loads: list[str] = []

    class FakeSentenceTransformer:
        def __init__(self, model):
            loads.append(model)

        def get_sentence_embedding_dimension(self):
            return 384

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=FakeSentenceTransformer))
    monkeypatch.setattr(embeddings, "_TRANSFORMERS", {})
    first = embeddings.create_semantic_provider("all-MiniLM-L6-v2")
    second = embeddings.create_semantic_provider("all-MiniLM-L6-v2")
    assert loads == ["all-MiniLM-L6-v2"]
    assert first._transformer is second._transformer


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


def test_relay_token_prefers_env_then_service_registry_key(monkeypatch):
    assert relay.resolve_token({"MEMORYMASTER_MCP_HTTP_TOKEN": " env-token "}) == "env-token"
    if relay.os.name != "nt":
        assert relay.resolve_token({}) == ""
        return
    import winreg

    opened = []

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, path):
        opened.append((root, path))
        return _Key()

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: (" reg-token ", winreg.REG_SZ))
    assert relay.resolve_token({}) == "reg-token"
    assert opened == [(winreg.HKEY_CURRENT_USER, relay.SHARED_KEY)]

    def missing(root, path):
        raise OSError("no key")

    monkeypatch.setattr(winreg, "OpenKey", missing)
    assert relay.resolve_token({}) == ""


def test_relay_bad_url_setting_becomes_an_error_reply_not_a_parse_error():
    request = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    [reply] = relay.forward(request, url="not a url", token="t", workspace="w")
    assert reply["id"] == 9 and "unreachable" in reply["error"]["message"]
