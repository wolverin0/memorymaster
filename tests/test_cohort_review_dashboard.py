"""The local review workspace never bypasses the dashboard's HTTP authority."""

import http.client
import re
import threading

from memorymaster.surfaces.dashboard import create_dashboard_server


def test_review_page_requires_auth_and_valid_host_without_serving_cohort_data(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_TOKEN_VIEWER", "review-fixture-token")
    monkeypatch.delenv("MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR", raising=False)
    monkeypatch.delenv("MEMORYMASTER_DASHBOARD_ALLOWED_ORIGINS", raising=False)
    server = create_dashboard_server(db_target=tmp_path / "fixture.db", workspace_root=tmp_path,
                                     host="127.0.0.1", port=0, operator_log_jsonl=tmp_path / "events.jsonl")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        client.request("GET", "/review")
        response = client.getresponse()
        assert response.status == 401
        response.read()
        headers = {"Authorization": "Bearer review-fixture-token"}
        client.request("GET", "/review", headers={**headers, "Host": "evil.example"})
        response = client.getresponse()
        assert response.status == 403
        response.read()
        client.request("GET", "/review", headers=headers)
        response = client.getresponse()
        page = response.read().decode("utf-8")
        assert response.status == 200
        assert response.getheader("Cache-Control") == "no-store"
        assert '__REVIEW_PACKET_JSON__' not in page
        assert 'application/json' in page
        assert re.search(r'<script[^>]*type="application/json"[^>]*>\s*null\s*</script>', page)
        client.request("POST", "/review", headers=headers, body="{}")
        response = client.getresponse()
        assert response.status == 403
        response.read()
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
