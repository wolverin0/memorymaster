"""Decision summary authorization and the candidate-to-retirement journey."""

import json
import runpy
from datetime import datetime, timedelta, timezone
import urllib.request

import pytest

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.operations.review_attempt import atomic_json
from memorymaster.surfaces.dashboard_summary import decision_summary

H = runpy.run_path("tests/test_dreaming_worker.py")
D = runpy.run_path("tests/test_capture_inbox_dashboard.py")


def test_summary_scopes_and_pending_historical_split(tmp_path):
    service = H["_service"](tmp_path)
    ledger = DreamLedger(tmp_path / "ledger.db")
    for scope in ("project:test", "project:other"):
        capture = H["_capture"](ledger, scope=scope, session_hash=scope)
        ledger.set_extraction(capture, [], "fixture")
        ledger.defer_consolidation(capture, "fixture")
    old = H["_capture"](ledger, session_hash="old")
    with ledger._connect() as conn:
        conn.execute("UPDATE dream_captures SET state='extracted',resume_eligible=0 WHERE id=?", (old,))
    result = decision_summary(service, scopes=["project:test"], ledger=ledger.db_path, review_root=tmp_path)
    assert result["capture"]["resumable"] == 1
    assert result["capture"]["deferred"] == 1
    assert result["capture"]["historical"] == 1
    assert result["capture"]["captures"] == 2
    assert result["profile"]["status"] == "unavailable"
    assert result["review"]["verdict"] == "UNAVAILABLE"
    service.require_tenant = True
    service._require_bound_authority = lambda: ("fixture", {"project:test"})
    with pytest.raises(PermissionError):
        decision_summary(service, scopes=["project:other"], ledger=ledger.db_path)


def test_summary_empty_error_and_stale_are_distinct(tmp_path):
    service = H["_service"](tmp_path)
    result = decision_summary(service, ledger=tmp_path / "missing", review_root=tmp_path)
    assert result["capture"]["status"] == "empty"
    assert result["profile"]["status"] == "empty"
    assert result["review"]["verdict"] == "INCOMPLETE"
    ledger = tmp_path / "broken"
    ledger.write_text("not sqlite")
    assert decision_summary(service, ledger=ledger, review_root=tmp_path)["capture"]["status"] == "error"
    now = datetime.now(timezone.utc)
    atomic_json(tmp_path / "attempt.json", {"attempt_id": "x", "started_at": (now-timedelta(hours=2)).isoformat(),
        "deadline_at": (now-timedelta(hours=1)).isoformat(), "completed_at": (now-timedelta(hours=1)).isoformat(),
        "outcome": "PASS", "interval_seconds": 60})
    atomic_json(tmp_path / "latest.json", {"attempt_id": "x", "verdict": "PASS"})
    assert decision_summary(service, ledger=ledger, review_root=tmp_path)["review"]["verdict"] == "STALE"


def test_dashboard_candidate_confirmed_cited_then_retired(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DASHBOARD_LEGACY_LOCAL", "1")
    service, db, workspace, receipt, claim = D["_fixture"](tmp_path)
    service.store.apply_status_transition(claim, to_status="candidate", reason="fixture reset", event_type="transition")
    with D["_server"](service, db, workspace) as base:
        def get(route):
            with urllib.request.urlopen(base+route) as response:
                return json.loads(response.read())
        search = "/api/retrieval?query=Alice&scope_allowlist=project:inbox&mode=legacy"
        assert get("/api/claims")["claims"][0]["status"] == "candidate"
        assert get(search)["rows"] == 0
        claim = service.store.get_claim(claim.id)
        service.store.apply_status_transition(claim, to_status="confirmed", reason="fixture validation", event_type="validator")
        rows = get(search)["rows_data"]
        assert rows[0]["claim"]["citations"] and rows[0]["claim"]["id"] == claim.id
        assert get(search.replace("project:inbox", "project:other"))["rows"] == 0
        D["_post"](base+"/api/capture-inbox/retire", {"source_item_id": receipt.source_item["id"], "apply": True})
        assert get(search)["rows"] == 0
