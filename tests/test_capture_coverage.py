"""Content-free capture completeness and operator-health contracts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.capture.coverage import capture_coverage
from memorymaster.capture.repository import CaptureRepository, graph_job_content_hash
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


def _service(tmp_path: Path) -> MemoryService:
    service = MemoryService(tmp_path / "coverage.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _evidence(service: MemoryService, *, scope: str, text: str = "private body"):
    source = service.upsert_external_source(
        source_type="fixture", display_name="Coverage fixture"
    )
    digest = "a" * 64
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id=f"item-{scope}",
        item_type="text",
        text=text,
        payload_json={"scope": scope},
        content_hash=digest,
    )
    evidence = service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="text",
        text=text,
        content_hash=digest,
    )
    return item, evidence


def _complete_claim_job(repository: CaptureRepository, source_id: int) -> None:
    job, _ = repository.queue_job(
        source_item_id=source_id,
        content_hash="a" * 64,
        stage="extract_claims",
    )
    leased = repository.lease_jobs(
        owner="coverage", stages=("extract_claims",), limit=1
    )[0]
    assert leased.id == job.id
    repository.finish_job(job.id, status="completed")


def test_missing_claim_job_breaks_coverage_without_leaking_content(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _, evidence = _evidence(
        service,
        scope="project:wanted",
        text="TOP SECRET CAPTURE BODY",
    )

    report = capture_coverage(service, scope="project:wanted")

    assert report["status"] == "broken"
    assert report["coverage_complete"] is False
    assert report["anomalies"]["missing_claim_jobs"] == {
        "count": 1,
        "evidence_ids": [evidence.id],
    }
    assert "TOP SECRET CAPTURE BODY" not in json.dumps(report)


def test_coverage_is_scope_isolated(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _evidence(service, scope="project:other")

    report = capture_coverage(service, scope="project:wanted")

    assert report["status"] == "ok"
    assert report["counts"]["active_sources"] == 0
    assert report["anomalies"]["missing_claim_jobs"]["count"] == 0


def test_confirmed_claim_requires_current_graph_job(tmp_path: Path) -> None:
    service = _service(tmp_path)
    item, evidence = _evidence(service, scope="project:wanted")
    repository = CaptureRepository(service.store)
    _complete_claim_job(repository, item.id)
    claim = service.ingest(
        "Alice uses Atlas.",
        [CitationInput(source="fixture", locator=f"evidence:{evidence.id}")],
        scope="project:wanted",
    )
    repository.link_claim_evidence(claim_id=claim.id, evidence_item_id=evidence.id)
    claim = service.store.apply_status_transition(
        claim,
        to_status="confirmed",
        reason="coverage fixture",
        event_type="validator",
    )

    missing = capture_coverage(service, scope="project:wanted")
    assert missing["anomalies"]["missing_graph_jobs"] == {
        "count": 1,
        "claim_ids": [claim.id],
    }

    graph_job, _ = repository.queue_job(
        source_item_id=item.id,
        content_hash=graph_job_content_hash(claim.id, claim.updated_at),
        stage="extract_graph",
    )
    leased = repository.lease_jobs(
        owner="coverage", stages=("extract_graph",), limit=1
    )[0]
    repository.finish_job(leased.id, status="completed")
    assert graph_job.id == leased.id

    complete = capture_coverage(service, scope="project:wanted")
    assert complete["coverage_complete"] is True
    assert complete["status"] == "ok"


def test_confidence_update_does_not_invalidate_completed_graph_job(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    item, evidence = _evidence(service, scope="project:wanted")
    repository = CaptureRepository(service.store)
    _complete_claim_job(repository, item.id)
    claim = service.ingest(
        "Alice uses Atlas.",
        [CitationInput(source="fixture", locator=f"evidence:{evidence.id}")],
        scope="project:wanted",
    )
    repository.link_claim_evidence(claim_id=claim.id, evidence_item_id=evidence.id)
    claim = service.store.apply_status_transition(
        claim,
        to_status="confirmed",
        reason="coverage fixture",
        event_type="validator",
    )
    confirmation_hash = graph_job_content_hash(claim.id, claim.updated_at)
    job, _ = repository.queue_job(
        source_item_id=item.id,
        content_hash=confirmation_hash,
        stage="extract_graph",
    )
    leased = repository.lease_jobs(
        owner="coverage", stages=("extract_graph",), limit=1
    )[0]
    repository.finish_job(leased.id, status="completed")
    service.store.set_confidence(claim.id, 0.8, "routine revalidation")
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE claims SET updated_at=? WHERE id=?",
            ("2099-01-01T00:00:00+00:00", claim.id),
        )
        conn.commit()

    report = capture_coverage(service, scope="project:wanted")
    due = repository.due_confirmed_graph_claims(
        scope="project:wanted", limit=10
    )

    assert job.id == leased.id
    assert report["status"] == "ok"
    assert report["anomalies"]["missing_graph_jobs"]["count"] == 0
    assert len(due) == 1
    assert due[0]["updated_at"] == "2099-01-01T00:00:00+00:00"
    assert due[0]["graph_revision"] == claim.updated_at
    assert due[0]["job_content_hash"] == confirmation_hash
    assert due[0]["job_exists"] is True


def test_expired_lease_is_broken_and_partial_completion_needs_attention(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    item, _ = _evidence(service, scope="project:wanted")
    repository = CaptureRepository(service.store)
    job, _ = repository.queue_job(
        source_item_id=item.id,
        content_hash="a" * 64,
        stage="extract_claims",
    )
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with service.store.connect() as conn:
        conn.execute(
            """UPDATE capture_jobs SET status='leased', attempts=1,
               lease_owner='crashed', lease_expires_at=? WHERE id=?""",
            (past, job.id),
        )
        conn.commit()

    expired = capture_coverage(service, scope="project:wanted")
    assert expired["status"] == "broken"
    assert expired["anomalies"]["expired_leases"] == {
        "count": 1,
        "job_ids": [job.id],
    }

    repository.finish_job(
        job.id,
        status="completed",
        error_code="partial_provider_output",
        error_detail="one invalid row",
    )
    partial = capture_coverage(service, scope="project:wanted")
    assert partial["status"] == "attention"
    assert partial["anomalies"]["partial_completed"]["count"] == 1

