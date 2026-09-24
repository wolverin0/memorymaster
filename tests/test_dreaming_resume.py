"""Resume eligibility is an explicit migration boundary, never historical recovery."""

import json
import runpy
import sqlite3
from datetime import timedelta

from memorymaster.dreaming.history_inventory import inventory
from memorymaster.dreaming.ledger import DreamLedger, _SCHEMA
from memorymaster.dreaming.worker import DreamConfig, DreamWorker

H = runpy.run_path("tests/test_dreaming_worker.py")
NOW = H["NOW"]


def test_old_database_retained_and_old_reader_compatible(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA)
    ledger = object.__new__(DreamLedger)
    ledger.db_path = path
    old_id = H["_capture"](ledger)
    with ledger._connect() as conn:
        conn.execute("UPDATE dream_captures SET state='extracted',extraction_json='[]' WHERE id=?", (old_id,))
    old_row = ledger.get_capture(old_id)
    ledger = DreamLedger(path)
    assert {key: ledger.get_capture(old_id)[key] for key in old_row} == old_row
    before = ledger.get_capture(old_id)
    DreamLedger(path)  # reinstall/migration replay
    assert ledger.get_capture(old_id) == before
    assert ledger.eligible(idle_minutes=0, max_sessions=20, now=NOW) == []
    assert ledger.status()["pending_extractions"] == {"resumable": 0, "deferred_budget": 0, "historical_retained": 1}
    with ledger._connect() as conn:
        # Prior reader projection and selector continue to work after migration.
        assert conn.execute("SELECT id FROM dream_captures WHERE state IN ('captured','retryable')").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM dream_schema_versions").fetchone()[0] == 3
    # v2 adds the absolute failure count and v3 the Jev-held candidate count;
    # historical rows start at zero for both.
    assert ledger.get_capture(old_id)["error_count"] == 0
    assert ledger.get_capture(old_id)["held_count"] == 0
    result = inventory(path, scope="project:test", now=NOW)
    assert result["count"] == 1 and result["items"][0]["id"] == old_id
    assert result["items"][0]["origin"] == "unknown"
    assert inventory(path, scope="project:other", now=NOW)["count"] == 0
    assert ledger.get_capture(old_id) == before


def test_restart_after_extraction_reuses_payload_and_replay_is_noop(tmp_path):
    ledger = DreamLedger(tmp_path / "dream.db")
    capture_id = H["_capture"](ledger)
    other_id = H["_capture"](ledger, scope="project:other", session_hash="other")
    run_id = ledger.start_run(False, "fixture", "fixture", now=NOW)
    candidates = H["_Extractor"]().extract([], scope="project:test", capture_hash="fixture").candidates
    ledger.set_extraction(capture_id, [c.to_dict() for c in candidates], run_id)
    assert ledger.get_capture(capture_id)["resume_eligible"] == 1
    # Simulated crash after atomic extraction persistence, before consolidation.
    ledger = DreamLedger(ledger.db_path)
    service = H["_service"](tmp_path)
    worker = DreamWorker(ledger, service, H["_NoCall"](), H["_Consolidator"](), config=DreamConfig(), now=lambda: NOW)
    result = worker.run(apply_candidates=True, scope="project:test")
    assert result["applied"] == 1 and result["extracted"] == 0 and result["errors"] == 0
    assert ledger.get_capture(other_id)["state"] == "captured"
    assert worker.run(apply_candidates=True, scope="project:test")["candidate_writes"] == 0
    assert len(service.list_claims(limit=20, scope_allowlist=["project:test", "personal"])) == 2


def test_budget_deferred_diagnostic_and_empty_extraction(tmp_path):
    ledger = DreamLedger(tmp_path / "dream.db")
    capture_id = H["_capture"](ledger)
    ledger.set_extraction(capture_id, [], "crashed-run")
    ledger.defer_consolidation(capture_id, "budget-run")
    assert ledger.status()["pending_extractions"] == {"resumable": 1, "deferred_budget": 1, "historical_retained": 0}
    worker = DreamWorker(ledger, H["_service"](tmp_path), H["_NoCall"](), H["_NoCall"](),
                         config=DreamConfig(), now=lambda: NOW + timedelta(days=1))
    assert worker.run(apply_candidates=True)["applied"] == 1
    assert ledger.get_capture(capture_id)["extraction"] == []
    assert ledger.get_capture(capture_id)["extraction_run_id"] == "crashed-run"
    assert "Blue interfaces" not in json.dumps(inventory(ledger.db_path, scope="project:test", now=NOW))
