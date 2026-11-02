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
PUBLIC_MARKER = "T2070PUBLICOK"


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


def _start_dashboard(monkeypatch, tmp_path, *, tokens: bool):
    """Dashboard on an ephemeral loopback port, seeded with non-public claims.

    Every hidden claim carries its own marker so a leak names the claim.
    ``zebrafact`` is shared so text retrieval can match all of them.
    """
    from memorymaster.surfaces.dashboard import create_dashboard_server

    if tokens:
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
    svc = server.service
    svc.init_db()
    cite = [CitationInput(source="test://t2070")]

    def seed(text, *, status=None, pinned=False, **kw):
        kw.setdefault("scope", "project:ws")
        kw.setdefault("source_agent", "alice")
        claim = svc.ingest(f"zebrafact {text}", cite, **kw)
        with svc.store.connect() as conn:
            conn.execute(
                "UPDATE claims SET status = COALESCE(?, status), pinned = ? WHERE id = ?",
                (status, int(pinned), claim.id),
            )
            conn.commit()
        return claim.id

    ids = {}
    # F-1a: another team's sensitive claim (cross-scope), plus same-scope
    # private/sensitive claims by another agent in every state a route reads.
    ids["sensitive"] = seed(f"{MARKER} payroll", scope="project:other-team", visibility="sensitive")
    ids["private_conflicted"] = seed(
        f"{MARKER}PRIVCONF", visibility="private", status="conflicted", pinned=True,
        subject=f"{MARKER}privsubject", predicate="has", object_value="privobj",
    )
    ids["sensitive_confirmed"] = seed(
        f"{MARKER}SENSCONF", visibility="sensitive", status="confirmed",
        subject=f"{MARKER}senssubject", predicate="has", object_value="sensobj",
    )
    ids["public"] = seed(
        f"{PUBLIC_MARKER} public control", status="confirmed", source_agent="tester",
        subject=f"{PUBLIC_MARKER}pubsubject", predicate="has", object_value="pubobj",
    )
    # F-1b neighbours: a public claim superseded by a private one, and a
    # private claim superseded by a public one (chains cross the boundary).
    ids["public_head"] = seed(f"{PUBLIC_MARKER} chain head", status="confirmed", source_agent="tester")
    ids["private_successor"] = seed(f"{MARKER}SUCC", visibility="private", status="confirmed")
    ids["private_predecessor"] = seed(f"{MARKER}PRED", visibility="private", status="superseded")
    ids["public_tail"] = seed(f"{PUBLIC_MARKER} chain tail", status="confirmed", source_agent="tester")
    with svc.store.connect() as conn:
        conn.executemany(
            "UPDATE claims SET replaced_by_claim_id = ? WHERE id = ?",
            [(ids["private_successor"], ids["public_head"]), (ids["public_tail"], ids["private_predecessor"])],
        )
        conn.commit()
    ids["html"] = seed(
        "lineage <script>alert('t2070')</script> text",
        subject="<img src=x onerror=alert(1)>", predicate="has", object_value="<b>bold</b>",
        source_agent="tester",
    )
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, {
        "host": host, "port": port, "ids": ids, "service": svc,
        "sensitive_id": ids["sensitive"], "html_id": ids["html"],
        "db": tmp_path / "dash.db", "workspace": workspace,
    }


def _seed_many(d, count, text, *, status=None, pinned=False, confidence=0.99, **kw):
    """Bulk-seed claims that outrank every base seed (confidence DESC ordering)."""
    svc = d["service"]
    kw.setdefault("scope", "project:ws")
    kw.setdefault("visibility", "private")
    # One author per row: a non-public confirmed (agent, subject, predicate, scope)
    # tuple is unique, and many authors is also the realistic flood.
    ids = [
        svc.ingest(
            f"zebrafact {text} {i}", [CitationInput(source="test://t2070")],
            source_agent=f"flood{i}", **kw,
        ).id
        for i in range(count)
    ]
    with svc.store.connect() as conn:
        conn.executemany(
            "UPDATE claims SET status = COALESCE(?, status), pinned = ?, confidence = ? WHERE id = ?",
            [(status, int(pinned), confidence, cid) for cid in ids],
        )
        conn.commit()
    return ids


def _stop(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    """Token-mode dashboard (viewer + operator roles)."""
    server, thread, info = _start_dashboard(monkeypatch, tmp_path, tokens=True)
    try:
        yield info
    finally:
        _stop(server, thread)


@pytest.fixture
def legacy_dashboard(monkeypatch, tmp_path):
    """Legacy loopback mode: no tokens configured, every caller is the operator."""
    server, thread, info = _start_dashboard(monkeypatch, tmp_path, tokens=False)
    try:
        yield info
    finally:
        _stop(server, thread)


def _request(d, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
    conn = http.client.HTTPConnection(d["host"], d["port"], timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


# Every dashboard GET route that can carry claim content. Query strings widen the
# result as far as a viewer can (allow_sensitive, include_*, viewer-chosen scopes).
_CLAIM_ROUTES = [
    "/api/claims",
    "/api/claims?status=confirmed&limit=500",
    "/api/claims?allow_sensitive=1&include_archived=1&limit=500",
    "/api/conflicts?include_stale=1",
    "/api/review-queue?allow_sensitive=1&include_stale=1",
    "/api/v1/review-queue",
    "/api/retrieval?query=zebrafact&mode=legacy&allow_sensitive=1&include_stale=1&include_conflicted=1",
    "/api/retrieval?query=zebrafact&mode=legacy&scope_allowlist=project:other-team,project:ws",
    "/api/recall-analysis?query=zebrafact&mode=legacy&allow_sensitive=1&scope_allowlist=project:other-team,project:ws",
    "/api/namespaces",
    "/api/provenance",
    "/api/events",
    "/api/timeline",
    "/api/audit",
    "/api/decision-summary",
    "/api/observability",
    "/api/session-stats",
    "/api/capture-inbox",
    # /api/graph-observations is not swept: it reads system-generated observation
    # claims whose seeding needs the graph pipeline, so a sweep entry would be
    # vacuous. See AUDIT-T2070.md "What I did not check".
]


@pytest.mark.parametrize("path", _CLAIM_ROUTES)
def test_viewer_never_receives_non_public_claims(dashboard, path):
    """F-1a: private/sensitive claims of other agents stay out of every viewer read.

    The hidden claims cover cross-scope candidate, and same-scope
    conflicted+pinned (review queue, conflicts) and confirmed (retrieval,
    namespaces) states, so no route passes only because its status filter
    excluded the seed.
    """
    status, body = _request(dashboard, "GET", path, VIEWER)
    assert status == 200
    assert MARKER.encode() not in body


def test_viewer_still_sees_public_claims(dashboard):
    """Positive control: the viewer is filtered, not blanked."""
    for path in (
        "/api/claims",
        "/api/retrieval?query=zebrafact&mode=legacy",
        "/api/namespaces",
    ):
        status, body = _request(dashboard, "GET", path, VIEWER)
        assert status == 200
        assert PUBLIC_MARKER.encode() in body, path
    status, body = _request(dashboard, "GET", f"/claim/{dashboard['ids']['public']}/lineage", VIEWER)
    assert status == 200
    assert PUBLIC_MARKER.encode() in body


@pytest.mark.parametrize("fixture_name,headers", [("dashboard", OPERATOR), ("legacy_dashboard", {})])
def test_operator_and_legacy_mode_still_see_non_public_claims(request, fixture_name, headers):
    """Local (legacy) and operator behaviour is unchanged."""
    d = request.getfixturevalue(fixture_name)
    status, body = _request(d, "GET", "/api/claims?limit=500", headers)
    assert status == 200
    assert body.count(MARKER.encode()) >= 4
    status, body = _request(d, "GET", f"/claim/{d['ids']['sensitive']}/lineage", headers)
    assert status == 200
    assert MARKER.encode() in body


@pytest.mark.parametrize(
    "claim_key",
    ["sensitive", "private_conflicted", "sensitive_confirmed", "private_successor", "private_predecessor"],
)
def test_viewer_lineage_of_non_public_claim_is_404_without_content(dashboard, claim_key):
    """F-1b: unauthorized lineage is indistinguishable from a missing claim."""
    cid = dashboard["ids"][claim_key]
    status, body = _request(dashboard, "GET", f"/claim/{cid}/lineage", VIEWER)
    assert status == 404
    assert MARKER.encode() not in body
    missing_status, missing_body = _request(dashboard, "GET", "/claim/999999/lineage", VIEWER)
    assert missing_status == 404
    # Same page shape as a missing id (only the number differs): no existence oracle.
    assert body.replace(f"#{cid} ".encode(), b"#N ") == missing_body.replace(b"#999999 ", b"#N ")


@pytest.mark.parametrize("claim_key", ["public_head", "public_tail"])
def test_viewer_lineage_of_public_claim_omits_non_public_neighbours(dashboard, claim_key):
    """F-1b: a lineage chain that crosses the visibility boundary is cut there."""
    cid = dashboard["ids"][claim_key]
    status, body = _request(dashboard, "GET", f"/claim/{cid}/lineage", VIEWER)
    assert status == 200
    assert PUBLIC_MARKER.encode() in body
    assert MARKER.encode() not in body


def test_lineage_refuses_claim_of_another_tenant(dashboard):
    """F-1b: store.get_claim ignores tenant_id; the dashboard must not."""
    svc = dashboard["service"]
    svc.tenant_id = "tenant-a"
    with svc.store.connect() as conn:
        conn.execute("UPDATE claims SET tenant_id = 'tenant-b' WHERE id = ?", (dashboard["ids"]["public"],))
        conn.commit()
    status, body = _request(dashboard, "GET", f"/claim/{dashboard['ids']['public']}/lineage", OPERATOR)
    assert status == 404
    assert PUBLIC_MARKER.encode() not in body


CONFLICT_KW = {"subject": "csubj", "predicate": "has", "scope": "project:ws"}


def _conflict_pair(d):
    """Two public claims that conflict (same subject/predicate/scope), plus a
    private conflicted and a private confirmed claim on the same key."""
    svc = d["service"]
    cite = [CitationInput(source="test://t2070")]
    ids = {}
    for name, value, vis, agent, status in (
        ("pub_a", f"{PUBLIC_MARKER}CA", "public", "tester", "conflicted"),
        ("pub_b", f"{PUBLIC_MARKER}CB", "public", "tester", "confirmed"),
        ("priv_a", f"{MARKER}CPRIVA", "private", "alice", "conflicted"),
        ("priv_b", f"{MARKER}CPRIVB", "private", "alice", "confirmed"),
    ):
        ids[name] = svc.ingest(
            f"zebrafact conflict {value}", cite, object_value=value, visibility=vis,
            source_agent=agent, **CONFLICT_KW,
        ).id
        with svc.store.connect() as conn:
            conn.execute("UPDATE claims SET status = ? WHERE id = ?", (status, ids[name]))
            conn.commit()
    return ids


def test_viewer_conflict_group_keeps_public_pair_and_drops_private_rows(dashboard):
    """Two public claims conflict next to private ones: the group is the public pair.

    The base seed has no conflicting pair, so /api/conflicts used to return before
    the visibility filter on the grouped rows ran at all.
    """
    _conflict_pair(dashboard)
    status, body = _request(dashboard, "GET", "/api/conflicts?include_stale=1", VIEWER)
    assert status == 200
    assert f"{PUBLIC_MARKER}CA".encode() in body and f"{PUBLIC_MARKER}CB".encode() in body
    assert MARKER.encode() not in body
    status, body = _request(dashboard, "GET", "/api/conflicts?include_stale=1", OPERATOR)
    assert f"{MARKER}CPRIVB".encode() in body


def test_conflict_groups_stay_inside_the_service_tenant(dashboard):
    """The grouped rows come from a store-level read: it must carry the tenant filter."""
    svc = dashboard["service"]
    ids = _conflict_pair(dashboard)
    svc.tenant_id = "tenant-a"
    with svc.store.connect() as conn:
        conn.execute("UPDATE claims SET tenant_id = 'tenant-a' WHERE id = ?", (ids["pub_a"],))
        conn.execute("UPDATE claims SET tenant_id = 'tenant-b' WHERE id = ?", (ids["pub_b"],))
        conn.commit()
    status, body = _request(dashboard, "GET", "/api/conflicts?include_stale=1", OPERATOR)
    assert status == 200
    assert f"{PUBLIC_MARKER}CA".encode() in body
    assert f"{PUBLIC_MARKER}CB".encode() not in body


def test_private_conflicted_rows_cannot_fill_the_conflict_limit(dashboard):
    """Truncation: newer high-confidence private conflicts must not hide the public one."""
    _conflict_pair(dashboard)
    _seed_many(dashboard, 5, "flood", status="conflicted", subject="floodsubj", predicate="has")
    status, body = _request(dashboard, "GET", "/api/conflicts?limit=2&include_stale=1", VIEWER)
    assert status == 200
    assert f"{PUBLIC_MARKER}CA".encode() in body
    assert MARKER.encode() not in body


def test_private_rows_cannot_fill_the_conflict_group_window(dashboard):
    """Truncation of the grouped read (window max(limit*12, 200)): 205 private
    confirmed rows on the same key must not push the public peer out."""
    _conflict_pair(dashboard)
    _seed_many(dashboard, 205, "window", status="confirmed", **CONFLICT_KW)
    status, body = _request(dashboard, "GET", "/api/conflicts?limit=1", VIEWER)
    assert status == 200
    assert f"{PUBLIC_MARKER}CB".encode() in body
    assert MARKER.encode() not in body


def test_private_rows_cannot_fill_the_claims_limit(dashboard):
    _seed_many(dashboard, 5, "flood", status="confirmed", subject=f"{MARKER}flood", predicate="has")
    status, body = _request(dashboard, "GET", "/api/claims?limit=3", VIEWER)
    assert status == 200
    assert PUBLIC_MARKER.encode() in body
    assert MARKER.encode() not in body
    # namespaces carries structured fields only: the window must still be filled.
    status, body = _request(dashboard, "GET", "/api/namespaces?limit=3", VIEWER)
    assert status == 200
    assert b'"rows": 3' in body
    assert MARKER.encode() not in body


def test_private_rows_cannot_fill_the_review_queue_limit(dashboard):
    svc = dashboard["service"]
    pub = svc.ingest(
        "zebrafact reviewable public", [CitationInput(source="test://t2070")], scope="project:ws",
        source_agent="tester", subject=f"{PUBLIC_MARKER}RQ", predicate="has", object_value="x",
    ).id
    with svc.store.connect() as conn:
        conn.execute("UPDATE claims SET status = 'conflicted', pinned = 1, confidence = 0.1 WHERE id = ?", (pub,))
        conn.commit()
    _seed_many(dashboard, 5, "flood", status="conflicted", pinned=True, subject=f"{MARKER}rq", predicate="has")
    status, body = _request(dashboard, "GET", "/api/review-queue?limit=3", VIEWER)
    assert status == 200
    assert f"{PUBLIC_MARKER}RQ".encode() in body
    assert MARKER.encode() not in body


def test_viewer_capture_inbox_hides_private_claims_and_keeps_public(dashboard):
    """Capture lineage lists derived claims by id; it must apply the same rule."""
    from memorymaster import remember
    from memorymaster.capture import CaptureRepository

    receipt = remember(
        text="capture control", scope="project:ws", db=dashboard["db"], workspace=dashboard["workspace"],
    )
    repo = CaptureRepository(dashboard["service"].store)
    for key in ("public", "private_conflicted"):
        repo.link_claim_evidence(claim_id=dashboard["ids"][key], evidence_item_id=int(receipt.evidence["id"]))
    status, body = _request(dashboard, "GET", "/api/capture-inbox", VIEWER)
    assert status == 200
    assert PUBLIC_MARKER.encode() in body
    assert MARKER.encode() not in body
    status, body = _request(dashboard, "GET", "/api/capture-inbox", OPERATOR)
    assert MARKER.encode() in body


def test_viewer_provenance_is_aggregate_only(dashboard):
    """Positive control: /api/provenance carries per-agent counts, never claim text."""
    status, body = _request(dashboard, "GET", "/api/provenance", VIEWER)
    assert status == 200
    assert b"tester" in body
    assert MARKER.encode() not in body and PUBLIC_MARKER.encode() not in body


def test_unknown_or_missing_role_maps_to_the_restricted_principal():
    """Fail closed: only the operator role is unrestricted."""
    from memorymaster.surfaces.dashboard import DashboardRequestHandler
    from memorymaster.surfaces.dashboard_auth import VIEWER_PRINCIPAL, DashboardRole, requesting_agent_for

    assert requesting_agent_for(DashboardRole.OPERATOR) is None
    assert requesting_agent_for(DashboardRole.VIEWER) == VIEWER_PRINCIPAL
    assert requesting_agent_for(None) == VIEWER_PRINCIPAL
    assert requesting_agent_for("root") == VIEWER_PRINCIPAL  # type: ignore[arg-type]
    # A handler that never reached _enforce_auth starts restricted, not unrestricted.
    assert DashboardRequestHandler._requesting_agent == VIEWER_PRINCIPAL


def test_mobile_queue_restricts_on_an_empty_string_principal(dashboard):
    """`is not None`, not truthiness: an empty principal is still a principal."""
    from memorymaster.surfaces.dashboard_read_models import mobile_review_queue_payload

    restricted = mobile_review_queue_payload(
        dashboard["service"], limit=100, scope=None, cursor=None, requesting_agent="",
    )
    everything = mobile_review_queue_payload(dashboard["service"], limit=100, scope=None, cursor=None)
    assert len(restricted["queue"]) < len(everything["queue"])


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
