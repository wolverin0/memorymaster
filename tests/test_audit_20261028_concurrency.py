"""Concurrency/idempotency cases from AUDIT-2026-10-28.

Skipped tests are red bars for bugs documented in AUDIT-2026-10-28.md; each
skip reason names its risk id. Un-skip together with the production fix.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from memorymaster.capture.repository import CaptureRepository
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern import llm_steward
from memorymaster.govern.llm_steward import ExtractionResult, run_steward


def _service(tmp_path: Path, monkeypatch) -> MemoryService:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    svc = MemoryService(str(tmp_path / "audit.db"), workspace_root=tmp_path)
    svc.init_db()
    return svc


def _ingest(svc: MemoryService, text: str) -> int:
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="t", locator="t", excerpt=text)],
        scope="project:audit",
        claim_type="fact",
        source_agent="test",
    ).id


def _other_writer_can_write(db_path: str) -> bool:
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def _run_steward_probing_lock(svc: MemoryService, monkeypatch) -> list[bool]:
    db_path = svc.store.db_path
    probes: list[bool] = []

    def fake_extract(provider, api_key, model, claim_id, text, base_url="", key_rotator=None, use_llm_provider=False):
        # The LLM call is where the steward waits on the network (60 s timeout
        # plus retries). Other writers must not be locked out while it waits.
        probes.append(_other_writer_can_write(db_path))
        return ExtractionResult(claim_id=claim_id, extractions=[])

    monkeypatch.setattr(llm_steward, "extract_claim", fake_extract)
    run_steward(db_path, api_key="x", provider="gemini", limit=10, delay=0.0, auto_validate=False)
    return probes


def test_steward_llm_call_does_not_hold_write_lock_after_normal_claim(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    _ingest(svc, "first normal candidate fact about widgets")
    _ingest(svc, "second normal candidate fact about gadgets")

    probes = _run_steward_probing_lock(svc, monkeypatch)

    assert probes == [True, True]


@pytest.mark.skip(
    reason=(
        "AUDIT-2026-10-28 R4: llm_steward.run_steward archives a too-short "
        "candidate and `continue`s before the per-claim conn.commit(), so the "
        "write transaction stays open across the next claim's LLM call and "
        "every other writer gets 'database is locked'."
    )
)
def test_steward_llm_call_does_not_hold_write_lock_after_short_archive(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    _ingest(svc, "too short")  # < 10 chars stripped -> auto-archived
    _ingest(svc, "normal candidate fact about widgets and gadgets")

    probes = _run_steward_probing_lock(svc, monkeypatch)

    assert probes == [True]


# --- capture job leases -----------------------------------------------------


def _queued_job(tmp_path: Path):
    service = MemoryService(tmp_path / "capture.db", workspace_root=tmp_path)
    service.init_db()
    source = service.upsert_external_source(source_type="fixture", display_name="Audit fixture")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="item-audit",
        item_type="text",
        text="body",
        payload_json={"scope": "project:audit"},
        content_hash="a" * 64,
    )
    repository = CaptureRepository(service.store)
    job, _ = repository.queue_job(source_item_id=item.id, content_hash="a" * 64, stage="extract_claims")
    return repository, job


def test_expired_lease_is_released_to_another_worker(tmp_path):
    repository, job = _queued_job(tmp_path)
    assert [j.id for j in repository.lease_jobs(owner="worker-a", lease_seconds=1)] == [job.id]
    assert repository.lease_jobs(owner="worker-b") == []
    time.sleep(1.2)
    assert [j.id for j in repository.lease_jobs(owner="worker-b")] == [job.id]


@pytest.mark.skip(
    reason=(
        "AUDIT-2026-10-28 R5: CaptureRepository.finish_job takes no lease owner "
        "and updates WHERE id=? only; a completed job is re-opened by a late "
        "'retryable' finish from a stale worker and runs again."
    )
)
def test_finish_job_does_not_reopen_completed_job(tmp_path):
    repository, job = _queued_job(tmp_path)
    repository.lease_jobs(owner="worker-a")
    repository.finish_job(job.id, status="completed")

    late = repository.finish_job(job.id, status="retryable", error_code="timeout")

    assert late.status == "completed"
