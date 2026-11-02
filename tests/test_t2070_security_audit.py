"""T-2070 security audit: regression locks and defect records.

Two kinds of test live here (see AUDIT-T2070.md):

* **Locks** — plain tests pinning security guards that hold today and had no
  direct coverage. They must keep passing.
* **Defect records** — ``xfail(strict=True)`` tests that state the secure
  behaviour for a confirmed finding. They pass the suite today because the
  defect is real; they turn into a hard XPASS failure the moment the product
  is fixed, which is the signal to drop the marker.

Every test uses ``tmp_path`` databases only; nothing touches an operator DB.
"""
from __future__ import annotations

import http.client
import threading
from dataclasses import dataclass
from typing import Iterator

import pytest

from memorymaster.core.models import CitationInput

VIEWER = {"Authorization": "Bearer t2070-viewer"}
OPERATOR = {"Authorization": "Bearer t2070-operator"}
MARKER = "T2070VISMARKER"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> Iterator[None]:
    for var in (
        "MEMORYMASTER_DASHBOARD_TOKEN_VIEWER",
        "MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR",
        "MEMORYMASTER_DASHBOARD_UNSAFE_BIND",
        "MEMORYMASTER_DASHBOARD_ALLOWED_ORIGINS",
        "MEMORYMASTER_ALLOW_SENSITIVE_BYPASS",
        "MEMORYMASTER_WEBHOOK_URL",
        "MEMORYMASTER_WEBHOOK_SECRET",
        "MEMORYMASTER_STEWARD_CLASSIFIER_PATH",
        "MEMORYMASTER_STEWARD_CLASSIFIER_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# Dashboard: viewer role vs. sensitive-visibility claims (finding F-1)
# ---------------------------------------------------------------------------


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    """Token-mode dashboard on an ephemeral loopback port.

    Seeds one ``visibility="sensitive"`` claim authored by another agent in a
    scope that is not the dashboard workspace's scope, plus one claim whose
    subject/text carry HTML for the escaping lock.
    """
    from memorymaster.surfaces.dashboard import create_dashboard_server

    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "t2070-operator")
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER", "t2070-viewer")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    server = create_dashboard_server(
        db_target=tmp_path / "dash.db",
        workspace_root=workspace,
        host="127.0.0.1",
        port=0,
        operator_log_jsonl=tmp_path / "op.jsonl",
    )
    server.service.init_db()
    sensitive = server.service.ingest(
        f"{MARKER} payroll arrangement for the other team",
        [CitationInput(source="test://t2070")],
        scope="project:other-team",
        visibility="sensitive",
        source_agent="alice",
    )
    html_claim = server.service.ingest(
        "lineage <script>alert('t2070')</script> text",
        [CitationInput(source="test://t2070")],
        scope="project:ws",
        subject="<img src=x onerror=alert(1)>",
        predicate="has",
        object_value="<b>bold</b>",
        source_agent="tester",
    )
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"host": host, "port": port, "sensitive_id": sensitive.id, "html_id": html_claim.id}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(d, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
    conn = http.client.HTTPConnection(d["host"], d["port"], timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-T2070 F-1a: /api/claims returns visibility=sensitive claims from other "
    "scopes/agents to the viewer role (no scope_allowlist, no requesting_agent, "
    "is_sensitive_claim() only scans text).",
)
def test_viewer_claims_list_hides_sensitive_visibility_claims(dashboard):
    status, body = _request(dashboard, "GET", "/api/claims", VIEWER)
    assert status == 200
    assert MARKER.encode() not in body


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-T2070 F-1b: /claim/<id>/lineage renders any claim by id with no "
    "visibility, scope or sensitivity check.",
)
def test_viewer_lineage_page_hides_sensitive_visibility_claim(dashboard):
    status, body = _request(dashboard, "GET", f"/claim/{dashboard['sensitive_id']}/lineage", VIEWER)
    assert MARKER.encode() not in body or status in (403, 404)


@dataclass
class _FakeClaim:
    id: int
    text: str
    object_value: str | None = None
    subject: str | None = None
    predicate: str | None = None


class _FakeService:
    def __init__(self, claims):
        self._claims = claims

    def list_claims(self, **_kwargs):
        return list(self._claims)


def test_claims_payload_default_filters_redacted_claims():
    """Lock: without allow_sensitive the dashboard drops redacted payloads."""
    from memorymaster.surfaces.dashboard_read_models import claims_payload

    service = _FakeService([_FakeClaim(1, "token is [REDACTED:api_key] here"), _FakeClaim(2, "plain fact")])
    out = claims_payload(service, status=None, limit=10, include_archived=False,
                         allow_sensitive=False, serialize=lambda c: {"id": c.id})
    assert [row["id"] for row in out["claims"]] == [2]


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-T2070 F-1c: dashboard allow_sensitive=1 widens results without "
    "resolve_allow_sensitive_access(), unlike MCP list_claims and the service layer.",
)
def test_claims_payload_allow_sensitive_requires_bypass_gate():
    from memorymaster.surfaces.dashboard_read_models import claims_payload

    service = _FakeService([_FakeClaim(1, "token is [REDACTED:api_key] here")])
    out = claims_payload(service, status=None, limit=10, include_archived=False,
                         allow_sensitive=True, serialize=lambda c: {"id": c.id})
    assert out["claims"] == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/review-queue?allow_sensitive=1",
        f"/api/retrieval?q={MARKER}+payroll&allow_sensitive=1",
        "/api/events",
        "/api/timeline",
        "/api/conflicts",
    ],
)
def test_viewer_service_gated_routes_do_not_leak_sensitive_claim(dashboard, path):
    """Lock: routes that go through the service query gate stay clean."""
    status, body = _request(dashboard, "GET", path, VIEWER)
    assert status == 200
    assert MARKER.encode() not in body


def test_viewer_cannot_open_operator_stream(dashboard):
    status, body = _request(dashboard, "GET", "/api/operator/stream", VIEWER)
    assert status == 403
    assert b"role_required_operator" in body


@pytest.mark.parametrize("route", ["/api/capture-inbox/retire", "/api/decisions/review"])
def test_viewer_cannot_post_to_routes_missing_from_operator_route_list(dashboard, route):
    """Lock: POST routes absent from POST_OPERATOR_ROUTES are still operator-only."""
    status, body = _request(dashboard, "POST", route, {**VIEWER, "Content-Type": "application/json"}, b"{}")
    assert status == 403
    assert b"role_required_operator" in body


def test_dashboard_rejects_foreign_host_header(dashboard):
    """Lock: DNS-rebinding guard — a foreign Host header is refused before auth."""
    status, body = _request(dashboard, "GET", "/api/claims", {**OPERATOR, "Host": "evil.example:80"})
    assert status == 403
    assert b"host_mismatch" in body


def test_operator_post_with_foreign_origin_is_csrf_rejected(dashboard):
    status, body = _request(
        dashboard, "POST", "/api/capture-inbox/retire",
        {**OPERATOR, "Origin": "http://evil.example", "Content-Type": "application/json"}, b"{}",
    )
    assert status == 403
    assert b"csrf_origin_mismatch" in body


def test_lineage_page_escapes_claim_html(dashboard):
    """Lock: claim subject/object/text are HTML-escaped in the lineage page and SVG."""
    status, body = _request(dashboard, "GET", f"/claim/{dashboard['html_id']}/lineage", OPERATOR)
    assert status == 200
    assert b"<img src=x" not in body
    assert b"<script>alert" not in body
    assert b"<b>bold</b>" not in body
    assert b"&lt;img src=x" in body


# ---------------------------------------------------------------------------
# Webhook: scheme allowlist (SSRF / local file read guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://internal.example/x",
        "gopher://127.0.0.1:6379/_INFO",
        "FILE:///etc/hosts",
        "data:text/plain,hello",
    ],
)
def test_webhook_refuses_non_http_schemes_without_dispatch(monkeypatch, url):
    from memorymaster.core import webhook

    calls: list[object] = []

    def _no_dispatch(*args, **kwargs):
        calls.append(args)
        raise AssertionError("urlopen must not be reached for a refused scheme")

    monkeypatch.setattr(webhook.urllib.request, "urlopen", _no_dispatch)
    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_URL", url)
    assert webhook.fire_webhook("claim.created", {"id": 1}) is False
    assert calls == []


def test_webhook_http_scheme_is_dispatched(monkeypatch):
    """Positive control: the scheme guard is not refusing everything."""
    from memorymaster.core import webhook

    class _Resp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    seen: list[str] = []

    def _fake_urlopen(req, timeout):
        seen.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(webhook.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setenv("MEMORYMASTER_WEBHOOK_URL", "http://127.0.0.1:9/hook")
    assert webhook.fire_webhook("claim.created", {"id": 1}) is True
    assert seen == ["http://127.0.0.1:9/hook"]


# ---------------------------------------------------------------------------
# MCP open_dashboard: caller-chosen host probe (finding F-4)
# ---------------------------------------------------------------------------


class _RecordingConnection:
    instances: list["_RecordingConnection"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.requests = host, port, []
        _RecordingConnection.instances.append(self)

    def request(self, method, path, *args, **kwargs):
        self.requests.append((method, path))

    def getresponse(self):
        class _R:
            status = 200

            @staticmethod
            def read():
                return b'{"internal": "payload"}'

        return _R()

    def close(self):
        pass


@pytest.fixture
def open_dashboard_tool(monkeypatch):
    try:
        from memorymaster.surfaces import mcp_server
        from memorymaster.surfaces.mcp_server import open_dashboard
    except ImportError:
        pytest.skip("MCP not installed")
    _RecordingConnection.instances = []
    monkeypatch.setattr(mcp_server.http.client, "HTTPConnection", _RecordingConnection)
    return open_dashboard


def test_open_dashboard_only_issues_get_health(open_dashboard_tool):
    """Lock: whatever path the caller passes, the probe is a single GET /health."""
    open_dashboard_tool(host="127.0.0.1", port=8765, path="/../../admin?x=1", check_health=True)
    assert [c.requests for c in _RecordingConnection.instances] == [[("GET", "/health")]]


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-T2070 F-4: open_dashboard connects to any caller-supplied host/port "
    "and returns the JSON body (internal probe / port-scan oracle).",
)
def test_open_dashboard_refuses_non_loopback_host(open_dashboard_tool):
    result = open_dashboard_tool(host="10.0.0.5", port=6379, check_health=True)
    assert _RecordingConnection.instances == []
    assert result.get("health_payload") is None


# ---------------------------------------------------------------------------
# Steward classifier: pickle (joblib) load is opt-in only (finding F-5)
# ---------------------------------------------------------------------------


def test_classifier_never_unpickles_without_opt_in(monkeypatch, tmp_path):
    """Lock: an artifact on the default path is not joblib-loaded unless enabled."""
    pytest.importorskip("joblib")
    import joblib

    from memorymaster.govern import steward_classifier

    artifact = tmp_path / "planted.joblib"
    artifact.write_bytes(b"not a real pickle")
    monkeypatch.setattr(steward_classifier, "_DEFAULT_PATH", artifact)
    loads: list[object] = []
    monkeypatch.setattr(joblib, "load", lambda *a, **k: loads.append(a) or {})
    steward_classifier.reset_cache()
    try:
        assert steward_classifier.load_classifier() is None
        assert loads == []
    finally:
        steward_classifier.reset_cache()
