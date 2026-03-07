from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService
from memorymaster.steward import list_steward_proposals, resolve_steward_proposal, run_steward


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def _case_workspace(prefix: str) -> Path:
    base = Path(".tmp_pytest")
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=str(base)))


def _force_status(db: Path, claim_id: int, status: str, updated_at: str) -> None:
    """Force a claim status for test seeding, bypassing uniqueness guards."""
    con = sqlite3.connect(str(db))
    con.execute("DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_update")
    con.execute("DROP TRIGGER IF EXISTS trg_claims_confirmed_tuple_guard_insert")
    con.execute("DROP INDEX IF EXISTS idx_claims_confirmed_tuple_unique")
    con.execute(
        "UPDATE claims SET status=?, updated_at=?, last_validated_at=? WHERE id=?",
        (status, updated_at, updated_at, claim_id),
    )
    con.commit()
    con.close()


def test_run_steward_manual_non_destructive_emits_proposals_and_artifact() -> None:
    db = _case_db("sqlite-steward-manual")
    workspace = _case_workspace("steward-workspace-manual")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()

    old_claim = service.ingest(
        text="API base URL is https://old.example.com",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="old url")],
        subject="api",
        predicate="base_url",
        object_value="https://old.example.com",
        confidence=0.7,
    )
    new_claim = service.ingest(
        text="API base URL is https://new.example.com",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="new url")],
        subject="api",
        predicate="base_url",
        object_value="https://new.example.com",
        confidence=0.8,
    )

    _force_status(db, old_claim.id, "confirmed", "2026-01-01T00:00:00+00:00")
    _force_status(db, new_claim.id, "confirmed", "2026-02-01T00:00:00+00:00")
    (workspace / "README.md").write_text("Current endpoint is https://new.example.com\n", encoding="utf-8")

    artifact = workspace / "artifacts" / "steward_report.json"
    report = run_steward(
        service,
        mode="manual",
        max_cycles=1,
        max_claims=10,
        max_proposals=10,
        max_probe_files=20,
        apply=False,
        artifact_path=artifact,
    )

    assert artifact.exists()
    assert report["cycles_completed"] == 1
    cycle = report["cycles"][0]
    by_id = {int(item["claim_id"]): item for item in cycle["decisions"]}
    assert old_claim.id in by_id
    assert by_id[old_claim.id]["decision"] == "superseded_candidate"
    reason_codes = {reason["code"] for reason in by_id[old_claim.id]["reasons"]}
    assert "relation.newer_confirmed_claim" in reason_codes

    persisted_old = service.store.get_claim(old_claim.id, include_citations=False)
    assert persisted_old is not None
    assert persisted_old.status == "confirmed"

    events = service.list_events(claim_id=old_claim.id, event_type="policy_decision", limit=20)
    assert events
    assert any((event.details or "").startswith("steward_proposal:") for event in events)


def test_run_steward_apply_can_transition_to_stale() -> None:
    db = _case_db("sqlite-steward-apply")
    workspace = _case_workspace("steward-workspace-apply")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()

    claim = service.ingest(
        text="Support pager is +1-555-0100",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="pager")],
        subject="support",
        predicate="pager",
        object_value="+1-555-0100",
        confidence=0.6,
    )
    _force_status(db, claim.id, "confirmed", "2026-01-02T00:00:00+00:00")

    report = run_steward(
        service,
        mode="manual",
        max_cycles=1,
        max_claims=10,
        max_proposals=10,
        max_probe_files=10,
        apply=True,
        artifact_path=workspace / "artifacts" / "steward_report_apply.json",
    )
    cycle = report["cycles"][0]
    by_id = {int(item["claim_id"]): item for item in cycle["decisions"]}
    assert by_id[claim.id]["decision"] == "stale"
    assert by_id[claim.id]["applied"] is True

    updated = service.store.get_claim(claim.id, include_citations=False)
    assert updated is not None
    assert updated.status == "stale"

    transitions = service.list_events(claim_id=claim.id, event_type="transition", limit=20)
    assert any(event.to_status == "stale" for event in transitions)


def test_run_steward_cadence_respects_budget_guardrails() -> None:
    db = _case_db("sqlite-steward-cadence")
    workspace = _case_workspace("steward-workspace-cadence")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()

    claim_ids: list[int] = []
    for idx in range(3):
        claim = service.ingest(
            text=f"Build host {idx} is host-{idx}.internal",
            citations=[CitationInput(source="session://chat", locator=f"turn-{idx}", excerpt="host")],
            subject="build",
            predicate="host",
            object_value=f"host-{idx}.internal",
            confidence=0.5,
        )
        claim_ids.append(claim.id)

    for idx, claim_id in enumerate(claim_ids):
        _force_status(db, claim_id, "confirmed", f"2026-01-0{idx + 1}T00:00:00+00:00")

    report = run_steward(
        service,
        mode="cadence",
        interval_seconds=0.01,
        max_cycles=2,
        max_claims=1,
        max_proposals=1,
        max_probe_files=5,
        apply=False,
        artifact_path=workspace / "artifacts" / "steward_report_cadence.json",
    )

    assert report["cycles_completed"] == 2
    for cycle in report["cycles"]:
        budget = cycle["budget"]
        assert budget["claims_scanned"] == 1
        assert budget["guardrails"]["max_claims_reached"] is True
        assert budget["proposals_emitted"] <= 1


def test_cli_run_steward_command_writes_report() -> None:
    db = _case_db("sqlite-steward-cli")
    workspace = _case_workspace("steward-workspace-cli")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()

    claim = service.ingest(
        text="Team lead is Maria Gomez",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="lead")],
        subject="team",
        predicate="lead",
        object_value="Maria Gomez",
        confidence=0.8,
    )
    _force_status(db, claim.id, "confirmed", "2026-01-01T00:00:00+00:00")

    artifact = workspace / "artifacts" / "steward_report_cli.json"
    cmd = [
        sys.executable,
        "-m",
        "memorymaster",
        "--db",
        str(db),
        "--workspace",
        str(workspace),
        "run-steward",
        "--mode",
        "manual",
        "--max-cycles",
        "1",
        "--max-claims",
        "5",
        "--artifact-json",
        str(artifact),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["cycles_completed"] == 1
    assert artifact.exists()


def test_steward_proposal_resolve_approve_and_reject_paths() -> None:
    db = _case_db("sqlite-steward-resolve")
    workspace = _case_workspace("steward-workspace-resolve")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()

    old_claim = service.ingest(
        text="Service endpoint is https://old.example.com",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="old")],
        subject="service",
        predicate="url",
        object_value="https://old.example.com",
        confidence=0.7,
    )
    new_claim = service.ingest(
        text="Service endpoint is https://new.example.com",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="new")],
        subject="service",
        predicate="url",
        object_value="https://new.example.com",
        confidence=0.9,
    )
    _force_status(db, old_claim.id, "confirmed", "2026-01-01T00:00:00+00:00")
    _force_status(db, new_claim.id, "confirmed", "2026-01-03T00:00:00+00:00")
    (workspace / "README.md").write_text("url https://new.example.com", encoding="utf-8")

    run_steward(
        service,
        mode="manual",
        max_cycles=1,
        max_claims=10,
        max_proposals=10,
        max_probe_files=10,
        apply=False,
        artifact_path=workspace / "artifacts" / "steward_resolve_report.json",
    )
    proposals = list_steward_proposals(service, limit=20, include_resolved=False)
    target = next(item for item in proposals if int(item["claim_id"]) == int(old_claim.id))
    approved = resolve_steward_proposal(
        service,
        action="approve",
        proposal_event_id=int(target["proposal_event_id"]),
        apply_on_approve=True,
    )
    assert approved["resolved"] is True
    assert approved["status"] == "approved"

    updated_old = service.store.get_claim(old_claim.id, include_citations=False)
    assert updated_old is not None
    assert updated_old.status == "superseded"

    # Emit another proposal and verify reject path remains non-destructive.
    stale_claim = service.ingest(
        text="Pager is +1-555-0199",
        citations=[CitationInput(source="session://chat", locator="turn-3", excerpt="pager")],
        subject="support",
        predicate="pager",
        object_value="+1-555-0199",
        confidence=0.6,
    )
    _force_status(db, stale_claim.id, "confirmed", "2026-01-05T00:00:00+00:00")
    run_steward(
        service,
        mode="manual",
        max_cycles=1,
        max_claims=10,
        max_proposals=10,
        max_probe_files=10,
        apply=False,
        artifact_path=workspace / "artifacts" / "steward_resolve_report_2.json",
    )
    pending = [
        item for item in list_steward_proposals(service, limit=20, include_resolved=False) if item["claim_id"] == stale_claim.id
    ]
    assert pending
    rejected = resolve_steward_proposal(
        service,
        action="reject",
        proposal_event_id=int(pending[0]["proposal_event_id"]),
    )
    assert rejected["resolved"] is True
    assert rejected["status"] == "rejected"
    same = service.store.get_claim(stale_claim.id, include_citations=False)
    assert same is not None
    assert same.status == "confirmed"


def test_steward_probe_timeout_and_circuit_breaker_flags_present() -> None:
    db = _case_db("sqlite-steward-timeout")
    workspace = _case_workspace("steward-workspace-timeout")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()

    for idx in range(3):
        claim = service.ingest(
            text=f"Build endpoint {idx} is https://svc-{idx}.example.com",
            citations=[CitationInput(source="session://chat", locator=f"turn-{idx}", excerpt="endpoint")],
            subject="build",
            predicate="url",
            object_value=f"https://svc-{idx}.example.com",
            confidence=0.5,
        )
        _force_status(db, claim.id, "confirmed", f"2026-01-0{idx + 1}T00:00:00+00:00")
    # Seed a file corpus to force filesystem loop work.
    corpus = workspace / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for idx in range(50):
        (corpus / f"f{idx}.txt").write_text("x" * 2000, encoding="utf-8")

    report = run_steward(
        service,
        mode="manual",
        max_cycles=1,
        max_claims=3,
        max_proposals=3,
        max_probe_files=500,
        probe_timeout_seconds=0.0001,
        probe_failure_threshold=1,
        apply=False,
        artifact_path=workspace / "artifacts" / "steward_timeout_report.json",
    )
    cycle = report["cycles"][0]
    guardrails = cycle["budget"]["guardrails"]
    assert float(guardrails["probe_timeout_seconds"]) == 0.0001
    assert int(guardrails["probe_failure_threshold"]) == 1
    assert int(guardrails["probe_circuit_open_count"]) >= 0

