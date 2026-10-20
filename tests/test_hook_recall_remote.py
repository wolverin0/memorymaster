"""T-0594: the recall hook asks the warm shared server instead of recalling cold.

Measured 2026-10-03: a ~2.1 s hook spent ~1.5 s opening the DB and building
caches; the same recall in a warm process took ~0.05 s, and under memory
pressure the cold path hit the 10 s hook timeout (96 cancellations in 3 days).
"""
from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest

from memorymaster.recall import remote

ENV = {"MEMORYMASTER_MCP_HTTP_TOKEN": "t", "MEMORYMASTER_SHARED_MCP_URL": "http://127.0.0.1:8766/mcp"}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _raises(exc):
    def opener(request, timeout):
        raise exc
    return opener


def test_route_url_is_derived_from_the_relay_url():
    assert remote.hook_recall_url(ENV) == "http://127.0.0.1:8766/hook/recall"


def test_answer_from_the_shared_server_with_bearer_and_hook_data():
    seen = {}

    def opener(request, timeout):
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        return _Response(b'{"ctx": "# Memory Context\\n- [mm-1] fact"}')

    answer = remote.remote_recall("why x", {"cwd": "G:/repo"}, environ=ENV, opener=opener, timeout=3)
    assert answer == remote.RemoteRecall("# Memory Context\n- [mm-1] fact", True)
    assert seen == {"auth": "Bearer t", "body": {"query": "why x", "hook_data": {"cwd": "G:/repo"}}, "timeout": 3}


@pytest.mark.parametrize(
    ("exc", "reachable"),
    [
        (urllib.error.URLError(ConnectionRefusedError(10061, "refused")), False),  # server down: recall locally
        (urllib.error.URLError(socket.timeout("timed out")), True),  # server busy: skip, no cold recall on top
        (TimeoutError("timed out"), True),
        (urllib.error.HTTPError("u", 404, "nf", {}, None), False),  # older server without the route
        (urllib.error.HTTPError("u", 500, "err", {}, None), True),
    ],
)
def test_failures_say_whether_the_server_got_the_request(exc, reachable):
    assert remote.remote_recall("q", None, environ=ENV, opener=_raises(exc)) == remote.RemoteRecall(None, reachable)


def test_disabled_or_tokenless_means_local(monkeypatch):
    assert remote.remote_recall("q", None, environ={**ENV, "MEMORYMASTER_HOOK_RECALL_REMOTE": "0"}) == (None, False)
    monkeypatch.setattr(remote, "resolve_token", lambda environ: "")
    assert remote.remote_recall("q", None, environ={}) == (None, False)


def test_hook_recall_falls_back_locally_only_when_the_server_was_not_reached(monkeypatch):
    calls = []
    import memorymaster.recall.context_hook as context_hook

    monkeypatch.setattr(context_hook, "recall", lambda q, **kw: calls.append(kw) or "local ctx")
    monkeypatch.setattr(remote, "remote_recall", lambda *a, **k: remote.RemoteRecall("warm ctx", True))
    assert remote.hook_recall("q", {"cwd": "x"}, db_path="db") == ("warm ctx", "shared")
    monkeypatch.setattr(remote, "remote_recall", lambda *a, **k: remote.RemoteRecall(None, True))
    assert remote.hook_recall("q", {"cwd": "x"}, db_path="db") == ("", "skipped_busy")
    assert calls == []
    monkeypatch.setattr(remote, "remote_recall", lambda *a, **k: remote.RemoteRecall(None, False))
    assert remote.hook_recall("q", {"cwd": "x"}, db_path="db") == ("local ctx", "local")
    assert calls == [{"db_path": "db", "skip_qdrant": True, "hook_data": {"cwd": "x"}}]


def _client(monkeypatch, mode):
    starlette_testclient = pytest.importorskip("starlette.testclient")
    from memorymaster.surfaces import mcp_http

    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", mode)
    app = mcp_http.create_http_app(token="secret", db_target="test.db", workspace=".")
    return starlette_testclient.TestClient(app)


def test_shared_server_route_serves_the_warm_recall_behind_the_token(monkeypatch):
    import memorymaster.recall.context_hook as context_hook

    got = {}
    monkeypatch.setattr(context_hook, "recall", lambda q, **kw: got.update(q=q, **kw) or "warm ctx")
    client = _client(monkeypatch, "local-trusted")
    body = {"query": "why x", "hook_data": {"cwd": "G:/repo"}}
    assert client.post("/hook/recall", json=body).status_code == 401
    response = client.post("/hook/recall", json=body, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200 and response.json() == {"ctx": "warm ctx"}
    assert got == {"q": "why x", "db_path": "test.db", "skip_qdrant": True, "hook_data": {"cwd": "G:/repo"}}
    bad = client.post("/hook/recall", content=b"[1]", headers={"Authorization": "Bearer secret"})
    assert bad.status_code == 400


def test_shared_server_stays_silent_for_a_switched_off_lane(monkeypatch, tmp_path):
    """T-1351: older hooks still ask the server; a lane switched off in recall-hook.json gets nothing."""
    import memorymaster.recall.context_hook as context_hook

    config = tmp_path / "recall-hook.json"
    config.write_text(json.dumps({"disabled_cwds": ["whatsappbot-final"]}), encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_RECALL_HOOK_CONFIG", str(config))
    monkeypatch.setattr(context_hook, "recall", lambda q, **kw: "warm ctx")
    client = _client(monkeypatch, "local-trusted")
    auth = {"Authorization": "Bearer secret"}
    off = client.post("/hook/recall", json={"query": "q", "hook_data": {"cwd": "G:/x/whatsappbot-final"}}, headers=auth)
    on = client.post("/hook/recall", json={"query": "q", "hook_data": {"cwd": "G:/x/memorymaster"}}, headers=auth)
    assert off.json() == {"ctx": ""} and on.json() == {"ctx": "warm ctx"}


def test_team_mode_server_has_no_hook_recall_route(monkeypatch):
    client = _client(monkeypatch, "team")
    response = client.post("/hook/recall", json={"query": "q"}, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 404


def test_shared_server_tells_the_worker_how_long_the_request_queued(monkeypatch):
    """T-1648: the limiter wait is part of the server's duration but was never separable from compute."""
    import memorymaster.recall.context_hook as context_hook
    from memorymaster.core import stall_probe

    seen = {}

    def recall(q, **kw):
        seen["wait"] = stall_probe.queue_wait_ms()
        return "warm ctx"

    monkeypatch.setattr(context_hook, "recall", recall)
    client = _client(monkeypatch, "local-trusted")
    response = client.post("/hook/recall", json={"query": "why x"}, headers={"Authorization": "Bearer secret"})
    assert response.json() == {"ctx": "warm ctx"}
    assert seen["wait"] is not None and seen["wait"] >= 0
    assert stall_probe.queue_wait_ms() is None  # the worker thread is clean for the next request


def test_shared_server_main_starts_the_stall_heartbeat(monkeypatch):
    from memorymaster.core import stall_probe
    from memorymaster.surfaces import mcp_http

    started = []
    monkeypatch.setattr(stall_probe, "start_heartbeat", lambda *a, **k: started.append(True))
    monkeypatch.setattr(mcp_http, "create_http_app", lambda **kw: object())
    monkeypatch.setattr(mcp_http, "_start_tokenizer_warmer", lambda db: None)
    monkeypatch.setattr(mcp_http, "_preload_native_ml", lambda: None)
    monkeypatch.setattr(mcp_http, "_limit_native_threads", lambda: None)
    monkeypatch.setattr(mcp_http, "configure_structured_logging", lambda *a, **k: None)
    monkeypatch.setattr(mcp_http, "_serve", lambda app, host, port: None)
    assert mcp_http.main(["--db", "test.db"]) == 0
    assert started == [True]
