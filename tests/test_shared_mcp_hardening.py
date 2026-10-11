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
    workspace = "G:/repos/alpha" if os.name == "nt" else "/repos/alpha"  # absolute on this platform
    token = _over_http({"x-mm-workspace": workspace})
    try:
        assert guarded() == workspace
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
            seen["running"] = isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)

    monkeypatch.setattr(mcp_http.os, "name", "nt")
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    mcp_http._serve(object(), host="127.0.0.1", port=1)  # really runs: also proves 3.10/3.11 compatibility
    assert seen == {"loop": "none", "running": True}


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


def test_supervisor_waits_out_a_paging_stall_before_killing(launcher, monkeypatch, tmp_path):
    # 2026-10-03: a paged-out server stopped answering for ~33 s and recovered on its own;
    # three failed probes (~45 s) killed it anyway, and the restart costs ~95 s of torch import.
    g = launcher["_supervise"].__globals__
    clock = {"now": 0.0}
    answers = iter([True] + [False] * 5 + [True] + [False] * 20)

    class Stop(Exception):
        pass

    class FakeChild:
        pid, returncode = 7, None

        def __init__(self, argv, **kwargs):
            if getattr(FakeChild, "started", False):
                raise Stop
            FakeChild.started = True

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9
            FakeChild.killed_at = clock["now"]

        def wait(self, timeout=None):
            return self.returncode

    def sleep(seconds):
        clock["now"] += seconds

    for name in ("MEMORYMASTER_MCP_HTTP_TOKEN", "MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST", "MEMORYMASTER_MCP_DB_ALLOWLIST"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("MEMORYMASTER_DEFAULT_DB", str(tmp_path / "memorymaster.db"))
    g["_single_instance"] = lambda: True
    g["_load_shared_environment"] = lambda: []
    g["_healthy"] = lambda: next(answers)
    g["time"] = SimpleNamespace(sleep=sleep, monotonic=lambda: clock["now"], strftime=lambda f, *_: "t", gmtime=lambda: None)
    g["subprocess"] = SimpleNamespace(Popen=FakeChild, DEVNULL=None, TimeoutExpired=TimeoutError)
    with pytest.raises(Stop):
        g["_supervise"]()
    # 50 s of failed probes is survived; the kill comes only after a sustained outage.
    assert FakeChild.killed_at - 70 >= g["UNHEALTHY_SECONDS_BEFORE_RESTART"] >= 120


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


@pytest.mark.skipif(os.name != "nt", reason="drive letters and case-insensitive roots are Windows path semantics")
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


# --- a stalled event loop leaves its stacks in the log before the watchdog kills it ---------


def _run_loop_for(seconds_stalled: float, monkeypatch, tmp_path):
    import asyncio
    import time

    from memorymaster.surfaces import mcp_http

    log = (tmp_path / "server.log").open("w+", encoding="utf-8")
    monkeypatch.setattr(mcp_http.sys, "stderr", log)
    monkeypatch.setattr(mcp_http, "_STALL_REARM_SECONDS", 0.05)

    async def main():
        watchdog = asyncio.create_task(mcp_http._loop_stall_dump(0.3))
        await asyncio.sleep(0.2)
        time.sleep(seconds_stalled)  # blocks the loop like a stuck call would
        await asyncio.sleep(0.1)
        watchdog.cancel()
        mcp_http._cancel_stall_dump()

    try:
        asyncio.run(main())
        log.flush()
        return (tmp_path / "server.log").read_text(encoding="utf-8")
    finally:
        log.close()


def test_a_stalled_loop_dumps_every_thread_stack(monkeypatch, tmp_path):
    dump = _run_loop_for(0.9, monkeypatch, tmp_path)
    assert "most recent call first" in dump and "_run_loop_for" in dump


def test_a_healthy_loop_dumps_nothing(monkeypatch, tmp_path):
    assert _run_loop_for(0.0, monkeypatch, tmp_path) == ""


def test_stall_dump_threshold_defaults_and_disables(monkeypatch):
    from memorymaster.surfaces import mcp_http

    monkeypatch.delenv(mcp_http.STALL_DUMP_ENV, raising=False)
    assert mcp_http._stall_dump_seconds() == 15.0
    monkeypatch.setenv(mcp_http.STALL_DUMP_ENV, "0")
    assert mcp_http._stall_dump_seconds() == 0.0


# --- T-1276: the supervisor must never start a second server over a live orphan ----------------
# 2026-10-11 01:19Z: the wedged server ignored child.kill() for 30 s, the supervisor logged
# "did not exit after kill; starting a new one anyway" and spawned replacements against the port the
# orphan still held: ~54 restarts, ~75 minutes without a shared server.


class _Expired(Exception):
    """Stands in for subprocess.TimeoutExpired inside the launcher's namespace."""


class _Stop(Exception):
    pass


def _orphan_rig(launcher, monkeypatch, tmp_path, *, dies_on_taskkill_call, kill_raises=None):
    """Supervisor wired to fakes: the first child ignores kill(); taskkill kills it on call N.

    ``dies_on_taskkill_call=None`` means the orphan survives every taskkill (unkillable).
    ``run()`` raises _Stop once the supervisor tries a second Popen or after the backoff sleeps
    pass ``state['max_backoffs']``.
    """
    g = launcher["_supervise"].__globals__
    clock = {"now": 0.0}
    state = {"popens": [], "taskkills": [], "backoff_sleeps": 0, "max_backoffs": 6,
             "alive_at_popen": [], "orphan_alive": True, "kill_calls": 0}

    class FakeChild:
        def __init__(self, argv, **kwargs):
            state["alive_at_popen"].append(state["orphan_alive"])
            if len(state["popens"]) >= 1:
                state["second_popen"] = True
                raise _Stop
            self.pid, self.argv = 4242, argv
            state["popens"].append(self)

        @property
        def returncode(self):
            return None if state["orphan_alive"] else -9

        def poll(self):
            return self.returncode

        def kill(self):
            state["kill_calls"] += 1  # TerminateProcess is accepted but the process stays wedged
            if kill_raises is not None:
                raise kill_raises

        def wait(self, timeout=None):
            if state["orphan_alive"]:
                raise _Expired(timeout)
            return self.returncode

    def run_cmd(argv, **kwargs):
        state["taskkills"].append(list(argv))
        if dies_on_taskkill_call is not None and len(state["taskkills"]) >= dies_on_taskkill_call:
            state["orphan_alive"] = False
        return SimpleNamespace(returncode=0)

    def sleep(seconds):
        clock["now"] += seconds
        if seconds >= g.get("KILL_RETRY_BACKOFF_SECONDS", 15) and state["orphan_alive"]:
            state["backoff_sleeps"] += 1
            if state["backoff_sleeps"] >= state["max_backoffs"]:
                raise _Stop

    for name in ("MEMORYMASTER_MCP_HTTP_TOKEN", "MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST", "MEMORYMASTER_MCP_DB_ALLOWLIST"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("MEMORYMASTER_DEFAULT_DB", str(tmp_path / "memorymaster.db"))
    monkeypatch.chdir(tmp_path.parent)
    g["_single_instance"] = lambda: True
    g["_load_shared_environment"] = lambda: []
    g["_healthy"] = lambda: False
    g["time"] = SimpleNamespace(sleep=sleep, monotonic=lambda: clock["now"], strftime=lambda f, *_: "t", gmtime=lambda: None)
    g["subprocess"] = SimpleNamespace(Popen=FakeChild, run=run_cmd, DEVNULL=None, TimeoutExpired=_Expired,
                                      CREATE_NO_WINDOW=0)
    return g["_supervise"], state


def test_supervisor_escalates_to_taskkill_and_only_then_replaces_a_wedged_server(launcher, monkeypatch, tmp_path):
    run, state = _orphan_rig(launcher, monkeypatch, tmp_path, dies_on_taskkill_call=1)
    with pytest.raises(_Stop):
        run()
    # kill() timed out -> taskkill /PID <pid> /F /T
    assert state["taskkills"][0][:3] == ["taskkill", "/PID", "4242"]
    assert {"/F", "/T"} <= set(state["taskkills"][0])
    assert state.get("second_popen") and state["alive_at_popen"] == [True, False]  # 2nd Popen saw a dead orphan


def test_supervisor_backs_off_without_spawning_while_the_orphan_survives_taskkill(launcher, monkeypatch, tmp_path):
    run, state = _orphan_rig(launcher, monkeypatch, tmp_path, dies_on_taskkill_call=None)
    with pytest.raises(_Stop):
        run()
    assert len(state["popens"]) == 1 and "second_popen" not in state  # never a second child over the held port
    assert state["backoff_sleeps"] >= 6  # kept retrying the kill loop instead of giving up
    assert len(state["taskkills"]) >= 2  # escalation is retried, not tried once


@pytest.mark.parametrize("error", [PermissionError("access denied"), ProcessLookupError("gone")])
def test_supervisor_survives_kill_raising_and_still_escalates_to_taskkill(launcher, monkeypatch, tmp_path, error):
    run, state = _orphan_rig(launcher, monkeypatch, tmp_path, dies_on_taskkill_call=2, kill_raises=error)
    with pytest.raises(_Stop):  # _Stop is the fake's second Popen: supervision kept running, not the OSError
        run()
    assert state["kill_calls"] >= 1 and len(state["taskkills"]) == 2  # kill() raised; taskkill still ran, retried
    assert state["alive_at_popen"] == [True, False]  # no second Popen while the orphan was alive


def test_supervisor_replaces_the_server_once_a_later_retry_finally_kills_it(launcher, monkeypatch, tmp_path):
    run, state = _orphan_rig(launcher, monkeypatch, tmp_path, dies_on_taskkill_call=3)
    with pytest.raises(_Stop):
        run()
    assert len(state["taskkills"]) == 3
    assert state["alive_at_popen"] == [True, False]


@pytest.mark.parametrize(
    "raised, expected",
    [(TimeoutError("timed out"), "timeout"), (ConnectionRefusedError(), "refused"),
     (urllib.error.HTTPError("u", 503, "x", {}, None), "http 503")],
)
def test_supervisor_health_probe_records_why_it_failed(launcher, monkeypatch, raised, expected):
    g = launcher["_healthy"].__globals__

    def fail(*args, **kwargs):
        raise raised

    g["urllib"] = SimpleNamespace(request=SimpleNamespace(urlopen=fail), error=urllib.error)
    assert launcher["_healthy"]() is False
    assert g["_LAST_PROBE_FAILURE"] == expected


@pytest.mark.skipif(os.name != "nt", reason="taskkill")
def test_kill_pid_really_kills_a_long_lived_process_tree(launcher):
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])  # our own throwaway child
    try:
        assert child.poll() is None
        launcher["_kill_pid"](child.pid)
        child.wait(timeout=20)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()


# --- T-1276: a slow decisions.db write never reaches /healthz ------------------------------------


def test_slow_decisions_write_on_the_shadow_thread_does_not_delay_healthz(monkeypatch, tmp_path):
    """REGRESSION GUARD, not a root-cause proof: the 2026-10-11 healthz stall stays unexplained.

    The stall dump showed a ``jev-recall-shadow`` thread parked in the ledger INSERT while the loop
    stopped answering /healthz, but the loop thread itself was in a trivial uvicorn call, so the write
    was never shown to block it. This pins the invariant that a blocked decisions.db writer on that
    thread (here: a real sqlite write lock held by another connection) cannot reach the loop.
    """
    import time

    import httpx

    import memorymaster.recall.context_hook as context_hook
    from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord
    from memorymaster.recall import jev_surfaces

    ledger = DecisionLedger(tmp_path / "decisions.db", busy_timeout_ms=4000)
    assert ledger.write_decision(DecisionRecord(decision_id="seed", surface="recall")) is True  # creates the schema
    blocker = sqlite3.connect(tmp_path / "decisions.db", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # another process holding the ledger write lock
    wrote: list[bool] = []

    def recall(query, **kwargs):  # what hook_recall -> jev_surfaces does in shadow mode
        jev_surfaces._start_shadow(lambda: wrote.append(
            ledger.write_decision(DecisionRecord(decision_id="slow", surface="recall"))))
        return "ctx"

    monkeypatch.setattr(context_hook, "recall", recall)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    app = mcp_http.create_http_app(token="secret", db_target=str(tmp_path / "m.db"), workspace=".")

    async def main() -> float:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            hook = await client.post("/hook/recall", json={"query": "q"}, headers={"Authorization": "Bearer secret"})
            assert hook.status_code == 200 and hook.json() == {"ctx": "ctx"}
            await anyio.sleep(0.3)
            assert jev_surfaces._SHADOW_THREADS[-1].is_alive()  # the writer really is stuck on the lock
            started = time.perf_counter()
            health = await client.get("/healthz")
            assert health.status_code == 200
            return time.perf_counter() - started

    try:
        elapsed = anyio.run(main)
        assert elapsed < 1.0, f"/healthz took {elapsed:.2f}s while the ledger write was blocked"
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        jev_surfaces.wait_shadow_decisions(10.0)
    assert wrote == [True]  # the write completed once the lock cleared
