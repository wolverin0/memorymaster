"""Budgeted replay-safe worker for capture text and claim extraction jobs."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

from memorymaster.capture.adapters import (
    CaptureRejected,
    CaptureRetryable,
    resolve_local_locator,
)
from memorymaster.capture.repository import CaptureRepository


@dataclass(frozen=True, slots=True)
class CaptureWorkerResult:
    leased: int
    completed: int
    retryable: int
    blocked: int
    errors: int
    partial: int


@dataclass(frozen=True, slots=True)
class CaptureJobCompletion:
    error_code: str
    error_detail: str


def capture_llm_overrides() -> dict[str, str]:
    """Resolve capture LLM settings without mutating the process environment."""
    provider = (
        os.environ.get("MEMORYMASTER_CAPTURE_LLM_PROVIDER", "").strip()
        or os.environ.get("MEMORYMASTER_LLM_PROVIDER", "").strip()
        or os.environ.get("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "").strip()
    )
    model = (
        os.environ.get("MEMORYMASTER_CAPTURE_LLM_MODEL", "").strip()
        or os.environ.get("MEMORYMASTER_LLM_MODEL", "").strip()
        or os.environ.get("MEMORYMASTER_DREAM_EXTRACT_MODEL", "").strip()
    )
    effort = (
        os.environ.get("MEMORYMASTER_CAPTURE_LLM_REASONING_EFFORT", "").strip()
        or os.environ.get("MEMORYMASTER_LLM_REASONING_EFFORT", "").strip()
        or os.environ.get("MEMORYMASTER_DREAM_EXTRACT_VARIANT", "").strip()
    )
    return {
        key: value
        for key, value in (
            ("MEMORYMASTER_LLM_PROVIDER", provider),
            ("MEMORYMASTER_LLM_MODEL", model),
            ("MEMORYMASTER_LLM_REASONING_EFFORT", effort),
        )
        if value
    }


def _payload(source: Any) -> dict[str, Any]:
    raw = source.payload_json or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _media_evidence(service: Any, source: Any, content_hash: str) -> Any:
    from memorymaster.bridges.media_providers import (
        get_ocr_provider,
        get_transcription_provider,
    )

    payload = _payload(source)
    locator = str(payload.get("locator") or "")
    provider_kind = str(payload.get("provider_kind") or "")
    path = resolve_local_locator(locator)
    if provider_kind == "ocr":
        name = os.environ.get("MEMORYMASTER_OCR_PROVIDER", "").strip()
        if not name:
            raise CaptureRejected("provider_unavailable", "Configure MEMORYMASTER_OCR_PROVIDER.")
        provider = get_ocr_provider(name)
        result = provider.extract(str(path))
    elif provider_kind == "transcription":
        name = os.environ.get("MEMORYMASTER_TRANSCRIPTION_PROVIDER", "").strip()
        if not name:
            raise CaptureRejected(
                "provider_unavailable", "Configure MEMORYMASTER_TRANSCRIPTION_PROVIDER."
            )
        provider = get_transcription_provider(name)
        result = provider.transcribe(str(path))
    else:
        raise CaptureRejected("unsupported_content_type", "No media provider route exists.")
    return service.add_evidence_item(
        source_item_id=source.id,
        evidence_type=result.evidence_type,
        text=result.text,
        media_path=locator,
        provider=result.provider,
        confidence=result.confidence,
        payload_json=result.payload_json,
        content_hash=content_hash,
    )


def _run_text_job(service: Any, repository: CaptureRepository, job: Any) -> None:
    source = service.get_source_item_by_id(job.source_item_id)
    if source is None or source.retired_at is not None:
        raise CaptureRejected("source_unavailable", "Source is missing or retired.")
    evidence = repository.evidence_for_content(
        source_item_id=source.id, content_hash=job.content_hash
    )
    if evidence is None:
        evidence = _media_evidence(service, source, job.content_hash)
    repository.queue_job(
        source_item_id=source.id,
        content_hash=job.content_hash,
        stage="extract_claims",
    )


def _run_claim_job(
    service: Any, repository: CaptureRepository, job: Any
) -> CaptureJobCompletion | None:
    evidence = repository.evidence_for_content(
        source_item_id=job.source_item_id, content_hash=job.content_hash
    )
    if evidence is None:
        raise CaptureRejected("missing_evidence", "Claim extraction requires evidence.")
    source = service.get_source_item_by_id(job.source_item_id)
    scope = str(_payload(source).get("scope") or "user") if source else "user"
    from memorymaster.bridges.atlas_llm_extractor import extract_atlas_claims_llm

    result = extract_atlas_claims_llm(
        service,
        scope=scope,
        limit=1,
        evidence_ids={evidence.id},
        evidence_items=[evidence],
    )
    if result.degraded:
        raise CaptureRetryable(
            "claim_provider_output_invalid",
            f"Claim provider output was unusable ({result.invalid_rows} invalid rows).",
        )
    if getattr(result, "partial", 0):
        return CaptureJobCompletion(
            "partial_provider_output",
            f"Preserved valid claims and rejected {result.invalid_rows} invalid rows.",
        )
    return None


def _process_job(
    service: Any, repository: CaptureRepository, job: Any
) -> CaptureJobCompletion | None:
    if job.stage == "extract_text":
        _run_text_job(service, repository, job)
        return None
    if job.stage == "extract_claims":
        from memorymaster.core.llm_provider import use_call_scoped_env

        with use_call_scoped_env(capture_llm_overrides()):
            return _run_claim_job(service, repository, job)
    if job.stage == "extract_graph":
        from memorymaster.knowledge.graph_extraction import (
            extract_confirmed_claim_graph,
        )
        from memorymaster.core.llm_provider import use_call_scoped_env

        with use_call_scoped_env(capture_llm_overrides()):
            extract_confirmed_claim_graph(service, repository, job)
        return None
    raise CaptureRejected("unsupported_stage", f"Unsupported stage: {job.stage}")


def run_capture_worker(
    service: Any,
    *,
    owner: str | None = None,
    limit: int = 25,
) -> CaptureWorkerResult:
    """Drain a bounded batch; no job can exceed repository retry limits."""
    repository = CaptureRepository(service.store)
    jobs = repository.lease_jobs(owner=owner or f"capture-{uuid.uuid4().hex}", limit=limit)
    counts = {
        "completed": 0,
        "retryable": 0,
        "blocked": 0,
        "errors": 0,
        "partial": 0,
    }
    for job in jobs:
        try:
            completion = _process_job(service, repository, job)
            repository.finish_job(
                job.id,
                status="completed",
                error_code=completion.error_code if completion else None,
                error_detail=completion.error_detail if completion else None,
            )
            counts["completed"] += 1
            counts["partial"] += int(completion is not None)
        except CaptureRejected as exc:
            repository.finish_job(
                job.id, status="blocked", error_code=exc.code, error_detail=exc.detail
            )
            counts["blocked"] += 1
        except CaptureRetryable as exc:
            finished = repository.finish_job(
                job.id,
                status="retryable",
                error_code=exc.code,
                error_detail=exc.detail,
            )
            counts[finished.status] += 1
            counts["errors"] += 1
        except Exception as exc:  # noqa: BLE001 - durable retry boundary
            finished = repository.finish_job(
                job.id,
                status="retryable",
                error_code="worker_error",
                error_detail=str(exc),
            )
            counts[finished.status] += 1
            counts["errors"] += 1
    return CaptureWorkerResult(leased=len(jobs), **counts)
