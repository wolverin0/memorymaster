"""Real HTTP origins and Host gates under authenticated and legacy loopback binds."""

import http.client
import threading
from email.message import Message

import pytest

from memorymaster.surfaces.dashboard import create_dashboard_server
from memorymaster.surfaces.dashboard_auth import BindUnsafeError, check_bind_safety, check_csrf, check_host
from memorymaster.surfaces.dashboard_origins import allowed_origins, normalize_origin


@pytest.fixture(autouse=True)
def clean_auth(monkeypatch):
    for suffix in ("TOKEN_OPERATOR", "TOKEN_VIEWER", "UNSAFE_BIND", "ALLOWED_ORIGINS"):
        monkeypatch.delenv("MEMORYMASTER_DASHBOARD_" + suffix, raising=False)


@pytest.mark.parametrize("origin", ["null", "", "https://evil-localhost:8765", "http://localhost.evil:8765",
                                  "http://localhost:8765@evil.example", "http://user@localhost:8765",
                                  "http://localhost:8765/path", "http://localhost:8765?", "http://localhost:8765#",
                                  "http://localhost:8765, http://evil", "http://localhost:99999", "http://localhost:",
                                  "http://localhost:0", "http://local host:8765", "http://[::1", "https://localhost:8765"])
def test_invalid_foreign_and_lookalike_origins_fail_closed(origin):
    assert not check_csrf({"Origin": origin}, configured_host_port="localhost:8765").ok


def test_referer_fallback_aliases_normalization_and_duplicates():
    assert normalize_origin("HTTP://LOCALHOST:80") == normalize_origin("http://localhost")
    assert check_csrf({"Referer": "http://localhost:8765/page?q=x"}, configured_host_port="127.0.0.1:8765").ok
    assert not check_csrf({"Origin": "null", "Referer": "http://localhost:8765/page"}, configured_host_port="127.0.0.1:8765").ok
    headers = Message()
    headers.add_header("Origin", "http://localhost:8765")
    headers.add_header("Origin", "http://evil.example")
    assert not check_csrf(headers, configured_host_port="localhost:8765").ok
    assert check_csrf({}, configured_host_port="localhost:8765").ok
    headers = Message()
    headers.add_header("Host", "localhost:8765")
    headers.add_header("Host", "evil.example")
    assert not check_host(headers, origins=allowed_origins("localhost", 8765)).ok


def test_wildcard_requires_explicit_allowlist_even_with_token(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "fixture")
    for host in ("", "0.0.0.0", "::"):
        with pytest.raises(BindUnsafeError, match="ALLOWED_ORIGINS"):
            check_bind_safety(host)
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_ALLOWED_ORIGINS", "https://dashboard.example")
    check_bind_safety("0.0.0.0")
    origins = allowed_origins("0.0.0.0", 8765)
    assert origins == frozenset({("https", "dashboard.example", 443)})
    assert not check_host({"Host": "localhost:8765"}, origins=origins).ok
    assert check_host({"Host": "dashboard.example"}, origins=origins).ok
    assert not check_host({"Host": "evil.example", "X-Forwarded-Host": "dashboard.example"}, origins=origins).ok


@pytest.mark.parametrize("authenticated", [False, True])
def test_effective_ephemeral_port_and_http_gates(tmp_path, monkeypatch, authenticated):
    if authenticated:
        monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "fixture-only")
    server = create_dashboard_server(db_target=tmp_path / "fixture.db", workspace_root=tmp_path,
                                     host="127.0.0.1", port=0, operator_log_jsonl=tmp_path / "events.jsonl")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        assert server.configured_host_port == f"127.0.0.1:{port}"
        base = {"Authorization": "Bearer fixture-only"} if authenticated else {}
        for origin, expected in ((f"http://localhost:{port}", 404), (f"http://evil-localhost:{port}", 403), ("null", 403)):
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request("POST", "/unregistered-fixture", body="{}", headers={**base, "Origin": origin})
            response = connection.getresponse()
            assert response.status == expected
            response.read()
            connection.close()
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", "/health", headers={"Host": f"evil-localhost:{port}"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
