"""Dashboard 'Decisions' tab: Jev metrics per surface and the weekly operator review queue.

Reads are viewer-level and never write the ledger; a Correct/Incorrect label is a
POST, so it needs the operator role and a same-origin request like every other
dashboard write, and it lands as an ``operator`` outcome in the decisions ledger.
"""
from __future__ import annotations

import http.client
import json
import re
import runpy
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memorymaster.decisions.ledger import DecisionLedger

Q = runpy.run_path(str(Path(__file__).with_name("test_jev_review_queue.py")))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER", "MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR",
                 "MEMORYMASTER_DASHBOARD_UNSAFE_BIND", "MEMORYMASTER_DASHBOARD_ALLOWED_ORIGINS",
                 "MEMORYMASTER_JEV_MODE", "MEMORYMASTER_JEV_RECALL", "MEMORYMASTER_JEV_DAILY_USD_CAP"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def dashboard(tmp_path, monkeypatch):
    from memorymaster.surfaces.dashboard import create_dashboard_server

    ledger_path = tmp_path / "decisions.db"
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(ledger_path))
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", "op-secret")
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER", "vw-secret")
    monkeypatch.setenv("MEMORYMASTER_JEV_RECALL", "live")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    server = create_dashboard_server(db_target=tmp_path / "dash.db", workspace_root=workspace, host="127.0.0.1",
                                     port=0, operator_log_jsonl=tmp_path / "op.jsonl")
    server.service.init_db()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield host, port, ledger_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _http(host, port, method, path, *, token="op-secret", origin=None, body=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
    if origin:
        headers["Origin"] = origin
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        return response.status, response.getheader("Content-Type") or "", response.read()
    finally:
        conn.close()


def _seeded(ledger_path):
    ledger = DecisionLedger(ledger_path)
    Q["seed"](ledger, datetime.now(timezone.utc))  # the server reads relative to its own clock
    return ledger


def _labels(ledger_path):
    return DecisionLedger(ledger_path).query("SELECT * FROM outcomes WHERE kind = 'operator_review'")


def test_decisions_page_is_linked_from_the_dashboard_and_renders(dashboard):
    host, port, _ = dashboard
    status, ctype, body = _http(host, port, "GET", "/dashboard", token="vw-secret")
    assert status == 200 and b'href="/decisions"' in body
    status, ctype, body = _http(host, port, "GET", "/decisions", token="vw-secret")
    assert status == 200 and ctype.startswith("text/html")
    html = body.decode("utf-8")
    for anchor in ('id="jev-surfaces"', 'id="jev-calibration"', 'id="jev-drift"', 'id="jev-review-queue"',
                   'id="jev-cost"', 'id="jev-exposure"', "/api/decisions/metrics", "/api/decisions/review-queue",
                   "/api/decisions/review"):
        assert anchor in html, anchor


def test_page_script_is_valid_javascript(dashboard, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    host, port, _ = dashboard
    _, _, body = _http(host, port, "GET", "/decisions", token="vw-secret")
    scripts = re.findall(r"<script>(.*?)</script>", body.decode("utf-8"), flags=re.S)
    assert scripts
    target = tmp_path / "decisions.js"
    target.write_text("\n".join(scripts), encoding="utf-8")
    result = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_metrics_endpoint_reports_per_surface_health_for_viewers(dashboard):
    host, port, ledger_path = dashboard
    _seeded(ledger_path)
    status, _, body = _http(host, port, "GET", "/api/decisions/metrics?days=30", token="vw-secret")
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    recall = payload["surfaces"]["recall"]
    assert recall["volume"] >= 40
    assert set(recall["latency_ms"]) >= {"p50", "p95", "p99"} and recall["engine_ms"]["p50"] == 450
    assert payload["configured_modes"]["recall"] == "live"
    assert payload["daily_usd_cap"] == 2.0
    assert "calibration" in payload and "drift" in payload and "cost_per_day" in payload


def test_review_queue_endpoint_lists_informative_decisions(dashboard):
    host, port, ledger_path = dashboard
    _seeded(ledger_path)
    status, _, body = _http(host, port, "GET", "/api/decisions/review-queue", token="vw-secret")
    assert status == 200
    items = json.loads(body)["items"]
    assert 0 < len(items) <= 20
    assert {"near", "disagree", "para", "steward"} <= {i["decision_id"] for i in items}


def test_operator_label_is_recorded_as_operator_outcome(dashboard):
    host, port, ledger_path = dashboard
    _seeded(ledger_path)
    origin = f"http://{host}:{port}"
    body = {"decision_id": "near", "item_ref": "claim:10", "question_id": "lifecycle.still_valid",
            "verdict": "correct"}
    status, _, raw = _http(host, port, "POST", "/api/decisions/review", origin=origin, body=body)
    assert status == 200, raw
    assert json.loads(raw)["ok"] is True
    rows = _labels(ledger_path)
    assert len(rows) == 1 and rows[0]["label_source"] == "operator" and rows[0]["value"] == 1.0
    status, _, raw = _http(host, port, "GET", "/api/decisions/review-queue?days=30", token="vw-secret")
    assert "near" not in {i["decision_id"] for i in json.loads(raw)["items"]}


@pytest.mark.parametrize("token,origin,expected", [
    ("vw-secret", None, 403),                      # viewers cannot label
    ("op-secret", "http://evil.example", 403),     # cross-origin browser write
    (None, None, 401),                             # anonymous
])
def test_label_requires_operator_and_same_origin(dashboard, token, origin, expected):
    host, port, ledger_path = dashboard
    _seeded(ledger_path)
    body = {"decision_id": "near", "item_ref": "claim:10", "question_id": "lifecycle.still_valid",
            "verdict": "correct"}
    status, _, _ = _http(host, port, "POST", "/api/decisions/review", token=token, origin=origin, body=body)
    assert status == expected
    assert _labels(ledger_path) == []


def test_invalid_label_is_a_client_error_and_writes_nothing(dashboard):
    host, port, ledger_path = dashboard
    _seeded(ledger_path)
    for body in ({"decision_id": "near", "item_ref": "claim:10", "question_id": "lifecycle.still_valid",
                  "verdict": "maybe"},
                 {"decision_id": "ghost", "item_ref": "claim:10", "question_id": "lifecycle.still_valid",
                  "verdict": "correct"},
                 {"decision_id": ["near"], "item_ref": "claim:10", "question_id": "q", "verdict": "correct"}):
        status, _, _ = _http(host, port, "POST", "/api/decisions/review", body=body)
        assert status == 400
    assert _labels(ledger_path) == []


def test_absent_ledger_reads_empty_and_is_never_created(dashboard):
    host, port, ledger_path = dashboard
    status, _, body = _http(host, port, "GET", "/api/decisions/metrics", token="vw-secret")
    assert status == 200 and json.loads(body)["surfaces"] == {}
    status, _, body = _http(host, port, "GET", "/api/decisions/review-queue", token="vw-secret")
    assert status == 200 and json.loads(body)["items"] == []
    assert not ledger_path.exists()


def test_unreadable_ledger_is_an_error_not_zeros(dashboard):
    host, port, ledger_path = dashboard
    ledger_path.write_text("not a sqlite database", encoding="utf-8")
    status, _, body = _http(host, port, "GET", "/api/decisions/metrics", token="vw-secret")
    assert status == 503
    assert json.loads(body)["ok"] is False
