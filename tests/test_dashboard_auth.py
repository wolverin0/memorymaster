"""Tests for v3.19.0-H2 dashboard auth + CSRF + bind-safety.

Two layers:
- Unit tests on the pure functions in memorymaster.dashboard_auth.
- End-to-end smoke that boots a DashboardHTTPServer on an ephemeral
  loopback port and exercises the HTTP gates with real headers.
"""
from __future__ import annotations

import http.client
import threading
from typing import Iterator

import pytest

from memorymaster import dashboard_auth
from memorymaster.dashboard_auth import (
    AuthDecision,
    BindUnsafeError,
    DashboardRole,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch) -> Iterator[None]:
    for var in (
        "MEMORYMASTER_DASHBOARD_TOKEN_VIEWER",
        "MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR",
        "MEMORYMASTER_DASHBOARD_UNSAFE_BIND",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# Unit: legacy_mode / authenticate / authorize
# ---------------------------------------------------------------------------


def test_legacy_mode_when_no_tokens_configured():
    assert dashboard_auth.legacy_mode() is True


def test_legacy_mode_off_when_any_token_set(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER", "secret-v")
    assert dashboard_auth.legacy_mode() is False


def test_authenticate_legacy_returns_operator_role():
    decision = dashboard_auth.authenticate({})
    assert decision.ok is True
    assert decision.role == DashboardRole.OPERATOR


def test_authenticate_missing_token_returns_401(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "op-secret")
    decision = dashboard_auth.authenticate({})
    assert decision.ok is False
    assert decision.status == 401
    assert decision.reason == "missing_token"


def test_authenticate_invalid_token_returns_401(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "op-secret")
    decision = dashboard_auth.authenticate({"Authorization": "Bearer wrong"})
    assert decision.ok is False
    assert decision.status == 401
    assert decision.reason == "invalid_token"


def test_authenticate_operator_token_returns_operator(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "op-secret")
    decision = dashboard_auth.authenticate({"Authorization": "Bearer op-secret"})
    assert decision.ok is True
    assert decision.role == DashboardRole.OPERATOR


def test_authenticate_viewer_token_returns_viewer(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER", "vw-secret")
    decision = dashboard_auth.authenticate({"Authorization": "Bearer vw-secret"})
    assert decision.ok is True
    assert decision.role == DashboardRole.VIEWER


def test_authorize_viewer_allowed_on_get(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER", "vw")
    base = dashboard_auth.authenticate({"Authorization": "Bearer vw"})
    decision = dashboard_auth.authorize(base, method="GET", route="/api/claims")
    assert decision.ok is True


def test_authorize_viewer_blocked_on_post(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER", "vw")
    base = dashboard_auth.authenticate({"Authorization": "Bearer vw"})
    decision = dashboard_auth.authorize(base, method="POST", route="/api/triage/action")
    assert decision.ok is False
    assert decision.status == 403
    assert decision.reason == "role_required_operator"


def test_authorize_viewer_blocked_on_operator_only_get(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER", "vw")
    base = dashboard_auth.authenticate({"Authorization": "Bearer vw"})
    decision = dashboard_auth.authorize(
        base, method="GET", route="/api/operator/stream"
    )
    assert decision.ok is False
    assert decision.status == 403


def test_authorize_operator_allowed_on_post(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "op")
    base = dashboard_auth.authenticate({"Authorization": "Bearer op"})
    decision = dashboard_auth.authorize(base, method="POST", route="/api/triage/action")
    assert decision.ok is True


# ---------------------------------------------------------------------------
# Unit: check_csrf
# ---------------------------------------------------------------------------


def test_csrf_legacy_mode_always_passes():
    decision = dashboard_auth.check_csrf({"Origin": "http://evil.example"}, configured_host_port="127.0.0.1:8765")
    assert decision.ok is True


def test_csrf_no_origin_header_passes(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "op")
    # curl/scripts don't send Origin — they should pass.
    decision = dashboard_auth.check_csrf({}, configured_host_port="127.0.0.1:8765")
    assert decision.ok is True


def test_csrf_origin_mismatch_returns_403(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "op")
    decision = dashboard_auth.check_csrf(
        {"Origin": "http://evil.example"},
        configured_host_port="127.0.0.1:8765",
    )
    assert decision.ok is False
    assert decision.status == 403
    assert decision.reason == "csrf_origin_mismatch"


def test_csrf_origin_match_passes(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "op")
    decision = dashboard_auth.check_csrf(
        {"Origin": "http://127.0.0.1:8765"},
        configured_host_port="127.0.0.1:8765",
    )
    assert decision.ok is True


# ---------------------------------------------------------------------------
# Unit: check_bind_safety
# ---------------------------------------------------------------------------


def test_bind_loopback_always_safe():
    dashboard_auth.check_bind_safety("127.0.0.1")
    dashboard_auth.check_bind_safety("localhost")
    dashboard_auth.check_bind_safety("::1")


def test_bind_non_loopback_refused_in_legacy_mode():
    with pytest.raises(BindUnsafeError):
        dashboard_auth.check_bind_safety("0.0.0.0")


def test_bind_non_loopback_allowed_with_auth_token(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "op")
    dashboard_auth.check_bind_safety("0.0.0.0")  # no raise


def test_bind_non_loopback_allowed_with_unsafe_opt_in(monkeypatch, caplog):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_UNSAFE_BIND", "1")
    import logging
    with caplog.at_level(logging.WARNING, logger="memorymaster.dashboard_auth"):
        dashboard_auth.check_bind_safety("0.0.0.0")
    assert any("running exposed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# End-to-end: boot server on ephemeral port, exercise HTTP gates
# ---------------------------------------------------------------------------


@pytest.fixture
def live_dashboard(monkeypatch, tmp_path) -> Iterator[tuple[str, int, threading.Thread, object]]:
    """Boot a DashboardHTTPServer on an ephemeral loopback port with op + viewer tokens.

    Yields (host, port, thread, server). The fixture sets both tokens so the
    server is in non-legacy mode, then yields control to the test.
    """
    from memorymaster.dashboard import create_dashboard_server

    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "op-secret")
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER", "vw-secret")

    db_path = tmp_path / "dash.db"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    operator_log = tmp_path / "op.jsonl"

    server = create_dashboard_server(
        db_target=db_path,
        workspace_root=workspace,
        host="127.0.0.1",
        port=0,  # ephemeral
        operator_log_jsonl=operator_log,
    )
    server.service.init_db()
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield host, port, thread, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _http(host, port, method, path, *, headers=None, body=None) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def test_e2e_anonymous_get_returns_401(live_dashboard):
    host, port, _, _ = live_dashboard
    status, _, body = _http(host, port, "GET", "/api/claims")
    assert status == 401
    assert b"missing_token" in body


def test_e2e_health_endpoint_exempt_from_auth(live_dashboard):
    host, port, _, _ = live_dashboard
    status, _, _ = _http(host, port, "GET", "/health")
    assert status == 200


def test_e2e_viewer_get_allowed(live_dashboard):
    host, port, _, _ = live_dashboard
    status, _, _ = _http(
        host, port, "GET", "/api/claims",
        headers={"Authorization": "Bearer vw-secret"},
    )
    assert status == 200


def test_e2e_viewer_post_returns_403(live_dashboard):
    host, port, _, _ = live_dashboard
    status, _, body = _http(
        host, port, "POST", "/api/triage/action",
        headers={"Authorization": "Bearer vw-secret", "Content-Length": "2"},
        body=b"{}",
    )
    assert status == 403
    assert b"role_required_operator" in body


def test_e2e_operator_post_csrf_mismatch_returns_403(live_dashboard):
    host, port, _, _ = live_dashboard
    status, _, body = _http(
        host, port, "POST", "/api/triage/action",
        headers={
            "Authorization": "Bearer op-secret",
            "Content-Length": "2",
            "Origin": "http://evil.example",
        },
        body=b"{}",
    )
    assert status == 403
    assert b"csrf_origin_mismatch" in body


def test_e2e_invalid_token_returns_401(live_dashboard):
    host, port, _, _ = live_dashboard
    status, _, body = _http(
        host, port, "GET", "/api/claims",
        headers={"Authorization": "Bearer not-the-secret"},
    )
    assert status == 401
    assert b"invalid_token" in body
