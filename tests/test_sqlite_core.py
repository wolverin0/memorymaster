from __future__ import annotations

import sqlite3
import tempfile
import os
from pathlib import Path

import pytest

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def test_sqlite_cycle_and_hybrid_retrieval():
    db = _case_db("sqlite-cycle")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    service.ingest(
        text="Server IP is 10.0.0.1",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="first ip")],
        subject="server",
        predicate="ip_address",
        object_value="10.0.0.1",
        volatility="high",
    )
    service.ingest(
        text="Server IP is 10.0.0.2",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="corrected ip")],
        subject="server",
        predicate="ip_address",
        object_value="10.0.0.2",
        volatility="high",
    )
    service.ingest(
        text="Credentials file path is C:\\secrets\\prod.env",
        citations=[CitationInput(source="session://chat", locator="turn-3", excerpt="credential path")],
        subject="workspace",
        predicate="path",
        object_value="C:\\secrets\\prod.env",
    )

    result = service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.5)
    assert result["validator"]["processed"] >= 3

    rows = service.query("server ip", retrieval_mode="hybrid", limit=10, allow_sensitive=True)
    assert any("10.0.0.2" in row.text for row in rows)
    assert all("Credentials file path" not in row.text for row in rows[:2])


def test_sensitive_redaction_and_access_control(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_ALLOW_SENSITIVE_BYPASS", "1")
    db = _case_db("sqlite-sensitive")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    claim = service.ingest(
        text="token=sk-abcdefghijklmnopqrstuv",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="token=sk-abcdefghijklmnopqrstuv")],
        subject="auth",
        predicate="token",
        object_value="sk-abcdefghijklmnopqrstuv",
    )

    stored = service.store.get_claim(claim.id)
    assert stored is not None
    assert "[REDACTED:" in stored.text

    service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.4)

    hidden = service.query("token", retrieval_mode="hybrid", limit=10, allow_sensitive=False, include_candidates=True)
    assert all(row.id != claim.id for row in hidden)

    visible = service.query("token", retrieval_mode="hybrid", limit=10, allow_sensitive=True, include_candidates=True)
    assert any(row.id == claim.id for row in visible)


def test_cadence_revalidation_selects_due_claim():
    db = _case_db("sqlite-cadence")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    claim = service.ingest(
        text="Server IP is 10.0.0.2",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="ip set")],
        subject="server",
        predicate="ip_address",
        object_value="10.0.0.2",
        volatility="high",
    )
    service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.5)

    con = sqlite3.connect(str(db))
    con.execute(
        "UPDATE claims SET last_validated_at='2025-01-01T00:00:00+00:00', updated_at='2025-01-01T00:00:00+00:00' WHERE id=?",
        (claim.id,),
    )
    con.commit()
    con.close()

    result = service.run_cycle(policy_mode="cadence", policy_limit=50, min_citations=1, min_score=0.5)
    assert result["policy"]["due"] >= 1
    assert result["validator"]["revalidation_processed"] >= 1


def test_list_events_filters_by_limit_event_type_and_claim_id():
    db = _case_db("sqlite-events")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    claim_a = service.ingest(
        text="Server region is us-east-1",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="region set")],
    )
    claim_b = service.ingest(
        text="Server region is eu-west-1",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="region update")],
    )

    service.store.record_event(claim_id=claim_a.id, event_type="audit", details="audit-a", payload={"k": "a"})
    service.store.record_event(claim_id=claim_a.id, event_type="sync", details="sync-a", payload={"k": "a2"})
    service.store.record_event(claim_id=claim_b.id, event_type="audit", details="audit-b", payload={"k": "b"})
    service.store.record_event(claim_id=None, event_type="system", details="system", payload={"scope": "global"})

    limited = service.list_events(limit=2)
    assert len(limited) == 2

    audit_events = service.list_events(event_type="audit", limit=10)
    assert len(audit_events) == 2
    assert all(evt.event_type == "audit" for evt in audit_events)
    assert {evt.claim_id for evt in audit_events} == {claim_a.id, claim_b.id}

    claim_a_events = service.list_events(claim_id=claim_a.id, limit=10)
    assert claim_a_events
    assert all(evt.claim_id == claim_a.id for evt in claim_a_events)

    claim_a_audit = service.list_events(claim_id=claim_a.id, event_type="audit", limit=10)
    assert len(claim_a_audit) == 1
    assert claim_a_audit[0].details == "audit-a"


def test_ingest_idempotency_key_dedupes_retries_without_extra_rows():
    db = _case_db("sqlite-idempotency")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    first = service.ingest(
        text="Server hostname is api.internal",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="initial ingest")],
        subject="server",
        predicate="hostname",
        object_value="api.internal",
        idempotency_key="retry-key-001",
    )
    second = service.ingest(
        text="Server hostname is api.internal (retry payload should be ignored)",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="retry ingest")],
        subject="server",
        predicate="hostname",
        object_value="api.internal",
        idempotency_key="retry-key-001",
    )

    assert first.id == second.id
    assert first.idempotency_key == "retry-key-001"
    assert second.idempotency_key == "retry-key-001"

    con = sqlite3.connect(str(db))
    claim_count = con.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    citation_count = con.execute("SELECT COUNT(*) FROM citations").fetchone()[0]
    ingest_event_count = con.execute("SELECT COUNT(*) FROM events WHERE event_type='ingest'").fetchone()[0]
    con.close()

    assert claim_count == 1
    assert citation_count == 1
    assert ingest_event_count == 1


def test_support_email_update_prefers_newer_candidate() -> None:
    db = _case_db("sqlite-email-update")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    service.ingest(
        text="Support email is ops@acme.example",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="original inbox")],
        subject="support",
        predicate="email",
        object_value="ops@acme.example",
    )
    service.ingest(
        text="Support email is help@acme.example (new inbox)",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="updated inbox")],
        subject="support",
        predicate="email",
        object_value="help@acme.example",
    )

    service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.5)
    claims = service.list_claims(limit=10, include_archived=True)
    status_by_object = {claim.object_value: claim.status for claim in claims}
    assert status_by_object["help@acme.example"] == "confirmed"
    assert status_by_object["ops@acme.example"] == "conflicted"


def test_deadline_update_prefers_newer_candidate() -> None:
    db = _case_db("sqlite-deadline-update")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    service.ingest(
        text="Release deadline is 2026-04-01",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="initial plan")],
        subject="release",
        predicate="deadline",
        object_value="2026-04-01",
    )
    service.ingest(
        text="Release deadline moved to 2026-04-15",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="updated plan")],
        subject="release",
        predicate="deadline",
        object_value="2026-04-15",
    )

    service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.5)
    claims = service.list_claims(limit=10, include_archived=True)
    status_by_object = {claim.object_value: claim.status for claim in claims}
    assert status_by_object["2026-04-15"] == "confirmed"
    assert status_by_object["2026-04-01"] == "conflicted"


def test_ingest_auto_generates_citation_when_empty() -> None:
    db = _case_db("sqlite-citation-auto")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    claim = service.ingest(
        text="Server host is api.internal",
        citations=[],
        subject="server",
        predicate="host",
        object_value="api.internal",
    )
    assert claim.id is not None
    assert len(claim.citations) == 1
    assert claim.citations[0].source == "mcp-session"

