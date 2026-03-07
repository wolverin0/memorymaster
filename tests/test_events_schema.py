from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def test_record_event_accepts_existing_valid_type() -> None:
    db = _case_db("events-schema-valid-record")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    service.store.record_event(
        claim_id=None,
        event_type="system",
        details="schema validation pass",
        payload={"scope": "global"},
    )

    events = service.list_events(event_type="system", limit=10)
    assert len(events) == 1
    assert events[0].details == "schema validation pass"


def test_record_event_rejects_invalid_type() -> None:
    db = _case_db("events-schema-invalid-record")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    with pytest.raises(ValueError, match="Invalid event_type 'not_a_real_event'"):
        service.store.record_event(
            claim_id=None,
            event_type="not_a_real_event",
            details="should fail",
        )


def test_status_transition_accepts_existing_valid_type() -> None:
    db = _case_db("events-schema-valid-transition")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    claim = service.ingest(
        text="Server host is api.internal",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="host")],
        subject="server",
        predicate="host",
        object_value="api.internal",
    )

    updated = service.store.apply_status_transition(
        claim,
        to_status="confirmed",
        reason="manual validation",
        event_type="validator",
    )

    assert updated.status == "confirmed"


def test_status_transition_rejects_invalid_type() -> None:
    db = _case_db("events-schema-invalid-transition")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    claim = service.ingest(
        text="Server host is api.internal",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="host")],
        subject="server",
        predicate="host",
        object_value="api.internal",
    )

    with pytest.raises(ValueError, match="Invalid event_type 'not_a_real_transition'"):
        service.store.apply_status_transition(
            claim,
            to_status="confirmed",
            reason="manual validation",
            event_type="not_a_real_transition",
        )

    current = service.store.get_claim(claim.id, include_citations=False)
    assert current is not None
    assert current.status == "candidate"


def test_status_transition_rejects_non_transition_event_type() -> None:
    db = _case_db("events-schema-wrong-transition-kind")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    claim = service.ingest(
        text="Server host is api.internal",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="host")],
        subject="server",
        predicate="host",
        object_value="api.internal",
    )

    with pytest.raises(ValueError, match="Invalid transition event_type 'system'"):
        service.store.apply_status_transition(
            claim,
            to_status="confirmed",
            reason="manual validation",
            event_type="system",
        )


def test_event_payload_schema_rejects_invalid_ingest_payload() -> None:
    db = _case_db("events-schema-invalid-ingest-payload")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    with pytest.raises(ValueError, match="payload missing required keys: citation_count"):
        service.store.record_event(
            claim_id=None,
            event_type="ingest",
            details="bad ingest",
            payload={"wrong_key": 1},
        )


def test_event_payload_schema_rejects_empty_policy_payload() -> None:
    db = _case_db("events-schema-invalid-policy-payload")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    with pytest.raises(ValueError, match="payload cannot be empty"):
        service.store.record_event(
            claim_id=None,
            event_type="policy_decision",
            details="policy",
            payload={},
        )


def test_event_payload_schema_rejects_triage_audit_without_source() -> None:
    db = _case_db("events-schema-invalid-triage-audit")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    with pytest.raises(ValueError, match="payload key 'source' must be non-empty string"):
        service.store.record_event(
            claim_id=None,
            event_type="audit",
            details="triage_mark_reviewed",
            payload={},
        )


def test_event_payload_schema_accepts_valid_compaction_run_payload() -> None:
    db = _case_db("events-schema-valid-compaction-run")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    service.store.record_event(
        claim_id=None,
        event_type="compaction_run",
        details="compaction_completed",
        payload={
            "retain_days": 30,
            "event_retain_days": 60,
            "archived_claims": 2,
            "deleted_events": 4,
            "artifacts": {
                "summary_graph": "artifacts/compaction/summary_graph.json",
                "traceability": "artifacts/compaction/traceability.json",
            },
        },
    )
    rows = service.list_events(event_type="compaction_run", limit=5)
    assert len(rows) == 1


@pytest.mark.parametrize(
    ("event_type", "details", "payload", "match"),
    [
        (
            "extractor",
            "structure_inferred",
            {"claim_type": "infra_fact", "subject": "server", "predicate": "ip_address"},
            "missing required keys: object_value",
        ),
        (
            "ingest",
            "ingest",
            {"citation_count": 0},
            "payload key 'citation_count' must be >= 1",
        ),
        (
            "compaction_run",
            "compaction_completed",
            {
                "retain_days": 30,
                "event_retain_days": 60,
                "archived_claims": 1,
                "deleted_events": 1,
                "artifacts": {"summary_graph": "a.json"},
            },
            "artifacts.traceability must be non-empty string",
        ),
        (
            "validator",
            "validation_pending_more_evidence",
            None,
            "requires payload for validation details",
        ),
        (
            "deterministic_validator",
            "checks",
            {"nested": {"bad": "shape"}},
            "must be JSON scalar",
        ),
        (
            "audit",
            "triage_suppress",
            {"source": ""},
            "payload key 'source' must be non-empty string",
        ),
    ],
)
def test_event_payload_contract_rejections_table_driven(
    event_type: str,
    details: str,
    payload: dict[str, object] | None,
    match: str,
) -> None:
    db = _case_db("events-schema-table")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    with pytest.raises(ValueError, match=match):
        service.store.record_event(
            claim_id=None,
            event_type=event_type,
            details=details,
            payload=payload,
        )

