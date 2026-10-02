"""Pre-install review of the shared MemoryMaster MCP server (T-0726, 2026-09-29).

Each test pins one defect an adversarial review reproduced against the shared
server before it was installed: core tools opening a DB relative to the process
cwd, sync tools blocking the one event loop, the Windows proactor listener
dying, fleet-wide rate buckets, non-Latin-1 workspace headers, the relay giving
up while the server starts, and a supervisor that must never die silently.
"""
from __future__ import annotations

import io
import json
import os
import runpy
import sqlite3
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import anyio
import pytest

from memorymaster.surfaces import mcp_http, mcp_path_policy, mcp_server
from memorymaster.surfaces import mcp_stdio_proxy as relay

LAUNCHER = Path(__file__).parents[1] / "integrations" / "shared-mcp" / "windows" / "memorymaster-mcp-shared.pyw"


@pytest.fixture
def local_trusted(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")


def _over_http(headers: dict[str, str]):
    from mcp.server.lowlevel.server import request_ctx

    return request_ctx.set(SimpleNamespace(request=SimpleNamespace(headers=headers)))


def _reset(token) -> None:
    from mcp.server.lowlevel.server import request_ctx

    request_ctx.reset(token)


# --- core tools must use the configured DB, never "<cwd>/memorymaster.db" ------------


def test_core_tools_use_the_configured_db_not_the_process_cwd(local_trusted, monkeypatch, tmp_path):
    configured = tmp_path / "authoritative" / "memorymaster.db"
    configured.parent.mkdir()
    elsewhere = tmp_path / "server-cwd"
    elsewhere.mkdir()
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_DB", str(configured))
    monkeypatch.chdir(elsewhere)

    receipt = mcp_server.remember(text="shared server core tool lands in the configured DB", workspace=str(tmp_path))
    assert receipt["ok"] is True
    mcp_server.recall(query="configured DB", workspace=str(tmp_path), retrieval_mode="legacy")

    assert not (elsewhere / "memorymaster.db").exists()  # the defect created this stray DB
    with sqlite3.connect(configured) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1


def test_core_tools_enforce_the_db_allowlist(local_trusted, monkeypatch, tmp_path):
    configured = tmp_path / "memorymaster.db"
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_DB", str(configured))
    monkeypatch.setenv(mcp_path_policy.ENV_DB_ALLOWLIST, str(configured))
    with pytest.raises(mcp_path_policy.MCPPathPolicyError):
        mcp_server.remember(text="x", db=str(tmp_path / "other.db"), workspace=str(tmp_path))
    assert not (tmp_path / "other.db").exists()


# --- sync tools leave the event loop over HTTP ----------------------------------------


def _probe_guarded():
    seen: dict[str, object] = {}

    def probe(workspace: str = ".") -> str:
        seen["thread"] = threading.get_ident()
        seen["http"] = mcp_server._current_http_request() is not None
        return workspace

    return mcp_server._off_event_loop_over_http(probe), seen


def test_http_tool_calls_run_off_the_event_loop_with_request_context():
    call, seen = _probe_guarded()

    async def main():
        token = _over_http({"x-mm-workspace": "G:/repos/alpha"})
        try:
            return await call(workspace="G:/repos/alpha"), threading.get_ident()
        finally:
            _reset(token)

    result, loop_thread = anyio.run(main)
    assert result == "G:/repos/alpha"
    assert seen["thread"] != loop_thread  # a slow tool can no longer stall every session
    assert seen["http"] is True  # request_ctx (and so X-MM-Workspace) is visible in the worker


def test_stdio_tool_calls_stay_inline():
    call, seen = _probe_guarded()

    async def main():
        return await call(), threading.get_ident()

    _result, loop_thread = anyio.run(main)
    assert seen["thread"] == loop_thread


def test_registered_tools_are_async_but_module_tools_stay_sync():
    import inspect

    registered = mcp_server.mcp._tool_manager.get_tool("classify_query")
    assert inspect.iscoroutinefunction(registered.fn)
    assert registered.fn.__mcp_action__ == "query"
    assert not inspect.iscoroutinefunction(mcp_server.classify_query)


def test_slow_http_tool_does_not_block_a_concurrent_one():
    import time

    def slow(workspace: str = ".") -> str:
        time.sleep(1.0)
        return "slow"

    def fast(workspace: str = ".") -> str:
        return "fast"

    slow_call = mcp_server._off_event_loop_over_http(slow)
    fast_call = mcp_server._off_event_loop_over_http(fast)
    finished: list[str] = []

    async def run(call):
        finished.append(await call())

    async def main():
        token = _over_http({"x-mm-workspace": "G:/repos/alpha"})
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(run, slow_call)
                await anyio.sleep(0.05)
                group.start_soon(run, fast_call)
        finally:
            _reset(token)

    anyio.run(main)
    assert finished == ["fast", "slow"]


# --- project_root is a client path too --------------------------------------------------


def test_project_root_takes_the_declared_workspace_and_fails_closed(local_trusted):
    def read_tasks(project_root: str = ".") -> str:
        return project_root

    guarded = mcp_server._authorized_tool_callable(read_tasks, mcp_server.McpToolPolicy("query", team_enabled=True))
    token = _over_http({"x-mm-workspace": "G:/repos/alpha"})
    try:
        assert guarded() == "G:/repos/alpha"
    finally:
        _reset(token)
    token = _over_http({})
    try:
        with pytest.raises(PermissionError, match="declare an absolute client workspace"):
            guarded()
    finally:
        _reset(token)
    assert guarded() == "."  # stdio unchanged


# --- non-Latin-1 workspace paths ---------------------------------------------------------


@pytest.mark.parametrize("path", ["C:/work/proj\u2013dash", "C:/work/\u043f\u0440\u043e\u0435\u043a\u0442", "C:/work/pro\u00f1a"])
def test_non_ascii_workspace_round_trips_through_the_header(path):
    encoded = relay.header_workspace(path)
    encoded.encode("latin-1")  # what http.client requires of a header value
    token = _over_http({"x-mm-workspace": encoded})
    try:
        assert mcp_server._client_workspace_header() == path
    finally:
        _reset(token)


def test_ascii_workspace_header_is_sent_verbatim():
    assert relay.header_workspace("G:/Py Apps/infra") == "G:/Py Apps/infra"


def test_encoded_placeholder_is_still_refused(local_trusted):
    guarded = mcp_server._authorized_tool_callable(
        lambda workspace=".": workspace, mcp_server.McpToolPolicy("query", team_enabled=True)
    )
    token = _over_http({"x-mm-workspace": "utf-8''" + quote("${CLAUDE_PROJECT_DIR}", safe="")})
    try:
        with pytest.raises(PermissionError, match="unexpanded config variable"):
            guarded()
    finally:
        _reset(token)


# --- rate limits are per client workspace on the shared server --------------------------


def test_one_sessions_burst_does_not_rate_limit_another_session(monkeypatch):
    monkeypatch.setenv("MM_INGEST_RATE_LIMIT_PER_MIN", "2")
    monkeypatch.setattr(mcp_server, "_INGEST_RATE_BUCKETS", {})
    monkeypatch.setattr(mcp_server, "_check_durable_ingest_quota", lambda agent, cost: None)

    def check(workspace: str):
        token = _over_http({"x-mm-workspace": workspace})
        try:
            return mcp_server._check_ingest_rate_limit("claude-session", now=100.0)
        finally:
            _reset(token)

    assert check("G:/repos/alpha") is None
    assert check("G:/repos/alpha") is None
    limited = check("G:/repos/alpha")
    assert limited is not None and limited["source_agent"] == "claude-session"
    assert check("G:/repos/beta") is None  # another pane under the same agent name still ingests


# --- relay survives the server starting after the client ----------------------------------


class _Response(io.BytesIO):
    def __init__(self, body: bytes):
        super().__init__(body)
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_relay_retries_a_refused_connection_until_the_server_is_up():
    attempts: list[int] = []
    slept: list[float] = []

    def opener(request, timeout):
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        return _Response(b'{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}}')

    raw = '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}'
    out = relay.forward(raw, url="http://127.0.0.1:1/mcp", token="t", workspace="w", opener=opener, sleep=slept.append)
    assert out[0]["result"]["protocolVersion"] == "2025-06-18"
    assert len(attempts) == 3 and slept == [1.0, 1.0]


def test_relay_gives_up_after_the_connect_budget():
    def refused(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    raw = '{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}'
    [reply] = relay.forward(raw, url="http://127.0.0.1:1/mcp", token="t", workspace="w",
                            opener=refused, connect_retry_seconds=3, sleep=lambda s: None)
    assert reply["error"]["code"] == -32603 and "unreachable" in reply["error"]["message"]


def test_relay_timeout_says_the_call_may_still_run():
    def slow(request, timeout):
        raise TimeoutError("timed out")

    raw = '{"jsonrpc": "2.0", "id": 3, "method": "tools/call"}'
    [reply] = relay.forward(raw, url="http://127.0.0.1:1/mcp", token="t", workspace="w", opener=slow)
    assert "may still be executing" in reply["error"]["message"]


# --- Windows serves on a selector loop ------------------------------------------------------


def test_windows_http_server_uses_a_selector_event_loop(monkeypatch):
    import asyncio

    import uvicorn

    seen = {}

    class FakeServer:
        def __init__(self, config):
            seen["loop"] = config.loop

        async def serve(self):
            return None

    def fake_run(coro, *, loop_factory=None):
        seen["factory"] = loop_factory
        coro.close()

    monkeypatch.setattr(mcp_http.os, "name", "nt")
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(asyncio, "run", fake_run)
    mcp_http._serve(object(), host="127.0.0.1", port=1)
    assert seen == {"loop": "none", "factory": asyncio.SelectorEventLoop}


# --- supervisor launcher ----------------------------------------------------------------------


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return runpy.run_path(str(LAUNCHER), run_name="launcher_under_test")


def test_launcher_refuses_to_start_without_its_config(launcher, monkeypatch, capsys):
    launcher["_single_instance"].__globals__["_single_instance"] = lambda: True
    launcher["_load_shared_environment"].__globals__["_load_shared_environment"] = lambda: ["MEMORYMASTER_MCP_HTTP_TOKEN"]
    assert launcher["_supervise"]() == 2
    assert "refusing to start, missing MEMORYMASTER_MCP_HTTP_TOKEN" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="winreg")
def test_launcher_missing_registry_key_reports_every_required_value(launcher, monkeypatch):
    import winreg

    def missing(root, path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(winreg, "OpenKey", missing)
    assert launcher["_load_shared_environment"]() == list(launcher["REQUIRED"])


def test_supervisor_restarts_a_dead_server_and_kills_an_unhealthy_one(launcher, monkeypatch, tmp_path):
    g = launcher["_supervise"].__globals__
    spawned: list["FakeChild"] = []

    class Stop(Exception):
        pass

    class FakeChild:
        def __init__(self, argv, **kwargs):
            if len(spawned) == 2:
                raise Stop
            self.argv, self.pid, self.returncode, self.killed = argv, 1000 + len(spawned), None, False
            self.polls = 0
            spawned.append(self)

        def poll(self):
            self.polls += 1
            if len(spawned) == 1 and self.polls > 1:
                self.returncode = 1  # first server crashes
            return self.returncode

        def kill(self):
            self.killed, self.returncode = True, -9

        def wait(self, timeout=None):
            return self.returncode

    for name in ("MEMORYMASTER_MCP_HTTP_TOKEN", "MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST", "MEMORYMASTER_MCP_DB_ALLOWLIST"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("MEMORYMASTER_DEFAULT_DB", str(tmp_path / "memorymaster.db"))
    monkeypatch.chdir(tmp_path.parent)
    g["_single_instance"] = lambda: True
    g["_load_shared_environment"] = lambda: []
    g["_healthy"] = lambda: False  # second server never answers
    g["time"] = SimpleNamespace(sleep=lambda s: None, monotonic=iter(range(0, 100000, 100)).__next__, strftime=lambda f, *_: "t", gmtime=lambda: None)
    g["subprocess"] = SimpleNamespace(Popen=FakeChild, DEVNULL=None, TimeoutExpired=TimeoutError)
    with pytest.raises(Stop):
        g["_supervise"]()
    assert Path.cwd() == tmp_path  # relative tool paths resolve next to the DB, never System32
    assert len(spawned) == 2
    assert "--serve" in spawned[0].argv and str(os.getpid()) in spawned[0].argv
    assert spawned[1].killed  # unhealthy after the startup grace -> killed, then replaced


def test_relay_connect_budget_is_wall_clock_not_attempts(monkeypatch):
    # Round-2 review: each refused connect takes ~2 s on Windows, so 60 attempts took ~185 s.
    clock = iter([0.0, 2.0, 4.0, 6.0, 8.0, 100.0, 100.0])
    monkeypatch.setattr(relay.time, "monotonic", lambda: next(clock))
    attempts: list[int] = []

    def refused(request, timeout):
        attempts.append(1)
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    raw = '{"jsonrpc": "2.0", "id": 4, "method": "tools/list"}'
    [reply] = relay.forward(raw, url="http://127.0.0.1:1/mcp", token="t", workspace="w",
                            opener=refused, connect_retry_seconds=10, sleep=lambda s: None)
    assert "unreachable" in reply["error"]["message"]
    assert len(attempts) == 5  # stopped at the 10 s deadline, not after 10 attempts


def _refused_opener(request, timeout):
    raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))


def test_relay_connects_while_the_server_boots_from_its_last_answers(tmp_path):
    # 2026-10-01: server boot took 56-144 s after logon, Claude's MCP connect timeout is 30 s,
    # so sessions opened during boot came up without memorymaster.
    cache = relay.ListingCache(str(tmp_path / "listings.json"))
    url = "http://127.0.0.1:1/mcp"
    tools = {"tools": [{"name": "query_memory", "inputSchema": {"type": "object"}}]}

    def up(request, timeout):
        method = json.loads(request.data)["method"]
        result = {"protocolVersion": "2025-06-18"} if method == "initialize" else tools
        return _Response(json.dumps({"jsonrpc": "2.0", "id": json.loads(request.data)["id"], "result": result}).encode())

    for i, method in enumerate(("initialize", "tools/list")):
        relay.forward(json.dumps({"jsonrpc": "2.0", "id": i, "method": method}), url=url, token="t",
                      workspace="w", opener=up, cache=cache)

    slept: list[float] = []
    booting = {"opener": _refused_opener, "sleep": slept.append, "cache": cache}
    [init] = relay.forward('{"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}}',
                           url=url, token="t", workspace="w", **booting)
    [listed] = relay.forward('{"jsonrpc": "2.0", "id": 8, "method": "tools/list"}',
                             url=url, token="t", workspace="w", **booting)
    assert init == {"jsonrpc": "2.0", "id": 7, "result": {"protocolVersion": "2025-06-18"}}
    assert listed["id"] == 8 and listed["result"] == tools
    assert slept == []  # answered at once, inside any client connect timeout


def test_relay_never_replays_tool_calls_or_cursor_pages(tmp_path):
    cache = relay.ListingCache(str(tmp_path / "listings.json"))
    url = "http://127.0.0.1:1/mcp"
    cache.put(url, "tools/list", {"tools": []})
    cache.put(url, "tools/call", {"content": "stale"})  # even if something stored it
    for raw in ('{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "q"}}',
                '{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"cursor": "p2"}}'):
        [reply] = relay.forward(raw, url=url, token="t", workspace="w", opener=_refused_opener,
                                connect_retry_seconds=0, sleep=lambda s: None, cache=cache)
        assert "unreachable" in reply["error"]["message"]


def test_relay_without_a_saved_answer_still_waits_for_the_server(tmp_path):
    cache = relay.ListingCache(str(tmp_path / "missing" / "listings.json"))
    [reply] = relay.forward('{"jsonrpc": "2.0", "id": 3, "method": "initialize"}', url="http://127.0.0.1:1/mcp",
                            token="t", workspace="w", opener=_refused_opener, connect_retry_seconds=0,
                            sleep=lambda s: None, cache=cache)
    assert "unreachable" in reply["error"]["message"]


def test_relay_waits_longer_than_the_slowest_measured_boot():
    assert relay.CONNECT_RETRY_SECONDS > 144


def test_relay_cache_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "listings.json"
    path.write_text("{torn", encoding="utf-8")
    cache = relay.ListingCache(str(path))
    assert cache.get("u", "tools/list") is None
    cache.put("u", "tools/list", {"tools": []})
    assert cache.get("u", "tools/list") == {"tools": []}


@pytest.mark.parametrize(
    ("workspace", "outside"),
    [
        ("G:/Py Apps/infra", False),
        ("G:/Py Apps", False),
        ("G:/py apps/_worktrees/x", False),
        ("C:/Users/me/orca/workspaces/a/b", False),
        ("C:/Users/me/Downloads", True),
        ("T:/claudecodetemp/x", True),
    ],
)
def test_relay_detects_sessions_outside_the_served_roots(workspace, outside):
    roots = "G:/Py Apps,G:/Py Apps/*,C:/Users/me/orca/workspaces/*"
    env = {"MEMORYMASTER_PROXY_ROOTS": roots.replace("/", os.sep)}
    assert relay.outside_served_roots(workspace.replace("/", os.sep), env) is outside
    assert relay.outside_served_roots(workspace, {}) is False  # no roots configured: always relay


def test_relay_outside_the_roots_runs_the_local_stdio_server(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORYMASTER_PROXY_ROOTS", str(tmp_path / "served") + "," + str(tmp_path / "served" / "*"))
    monkeypatch.setenv("MEMORYMASTER_PROXY_WORKSPACE", str(tmp_path / "elsewhere"))
    monkeypatch.delenv("MEMORYMASTER_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.setattr(relay, "resolve_token", lambda environ=None: "")
    monkeypatch.setattr(relay.sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.setattr(relay.sys, "stdout", io.TextIOWrapper(io.BytesIO()))
    monkeypatch.setattr(relay, "_run_local_server", lambda: 17)
    assert relay.main() == 17  # fell back before needing a token
