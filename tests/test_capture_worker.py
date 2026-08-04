from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from memorymaster.capture.producers import ProducerItem, normalize_producer_item
from memorymaster.capture.worker import run_capture_worker
from memorymaster.core.service import MemoryService
from memorymaster.public.v1 import remember


@pytest.fixture
def worker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = tmp_path / "worker.db"
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_ROOTS", f"worker={workspace}")
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_TRUST_MODE", "local-trusted")
    service = MemoryService(str(db), workspace_root=workspace)
    service.init_db()
    return service, db, workspace


def test_claim_worker_completes_with_bounded_extractor(worker_env, monkeypatch) -> None:
    service, db, workspace = worker_env
    remember(text="Worker claim evidence", db=db, workspace=workspace)
    seen = {}

    def fake_extract(_service, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(degraded=0)

    monkeypatch.setattr(
        "memorymaster.bridges.atlas_llm_extractor.extract_atlas_claims_llm",
        fake_extract,
    )
    result = run_capture_worker(service, owner="fixture", limit=1)
    assert result.completed == 1
    assert result.errors == 0
    assert len(seen["evidence_ids"]) == 1


def test_capture_jobs_inherit_dream_opencode_config_without_mutating_env(
    worker_env, monkeypatch
) -> None:
    service, db, workspace = worker_env
    remember(text="OAuth-backed worker evidence", db=db, workspace=workspace)
    monkeypatch.delenv("MEMORYMASTER_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MEMORYMASTER_LLM_MODEL", raising=False)
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "opencode")
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_MODEL", "openai/gpt-5.6-terra")
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_VARIANT", "medium")
    seen = {}

    def fake_extract(_service, **kwargs):
        from memorymaster.core.llm_provider import _env

        seen["provider"] = _env("MEMORYMASTER_LLM_PROVIDER")
        seen["model"] = _env("MEMORYMASTER_LLM_MODEL")
        seen["effort"] = _env("MEMORYMASTER_LLM_REASONING_EFFORT")
        return SimpleNamespace(degraded=0)

    monkeypatch.setattr(
        "memorymaster.bridges.atlas_llm_extractor.extract_atlas_claims_llm",
        fake_extract,
    )

    result = run_capture_worker(service, owner="fixture", limit=1)

    assert result.completed == 1
    assert seen == {
        "provider": "opencode",
        "model": "openai/gpt-5.6-terra",
        "effort": "medium",
    }
    assert "MEMORYMASTER_LLM_PROVIDER" not in os.environ
    assert "MEMORYMASTER_LLM_MODEL" not in os.environ


def test_sequential_jobs_extract_their_exact_evidence(worker_env, monkeypatch) -> None:
    service, db, workspace = worker_env
    remember(text="Project First uses SQLite.", db=db, workspace=workspace)
    remember(text="Project Second uses Redis.", db=db, workspace=workspace)

    def fake_call(_prompt: str, text: str) -> str:
        subject = "Project First" if "First" in text else "Project Second"
        technology = "SQLite" if "First" in text else "Redis"
        return json.dumps(
            [
                {
                    "type": "project",
                    "subject": subject,
                    "predicate": "uses",
                    "object": technology,
                    "text": f"{subject} uses {technology}.",
                    "confidence": 0.95,
                }
            ]
        )

    monkeypatch.setattr(
        "memorymaster.bridges.atlas_llm_extractor.call_llm", fake_call
    )

    first = run_capture_worker(service, owner="first", limit=1)
    second = run_capture_worker(service, owner="second", limit=1)

    assert first.completed == second.completed == 1
    with service.store.connect() as conn:
        rows = conn.execute(
            """SELECT c.object_value, l.evidence_item_id
               FROM claims c
               JOIN claim_evidence_links l ON l.claim_id=c.id
               ORDER BY l.evidence_item_id"""
        ).fetchall()
    assert [(row["object_value"], row["evidence_item_id"]) for row in rows] == [
        ("SQLite", 1),
        ("Redis", 2),
    ]


def test_provider_absence_blocks_media_with_actionable_code(worker_env) -> None:
    service, db, workspace = worker_env
    image = workspace / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    remember(path=image, db=db, workspace=workspace)
    result = run_capture_worker(service, owner="fixture", limit=1)
    assert result.blocked == 1
    with service.store.connect() as conn:
        job = conn.execute("SELECT status, error_code FROM capture_jobs").fetchone()
    assert tuple(job) == ("blocked", "provider_unavailable")


def test_llm_timeout_preserves_source_and_retries(worker_env, monkeypatch) -> None:
    service, db, workspace = worker_env
    receipt = remember(text="Retryable evidence", db=db, workspace=workspace)

    def timeout(*args, **kwargs):
        raise TimeoutError("provider timed out with token=secret-value")

    monkeypatch.setattr(
        "memorymaster.bridges.atlas_llm_extractor.extract_atlas_claims_llm",
        timeout,
    )
    result = run_capture_worker(service, owner="fixture", limit=1)
    assert result.retryable == 1
    assert service.get_source_item_by_id(receipt.source_item["id"]) is not None
    with service.store.connect() as conn:
        job = conn.execute(
            "SELECT status, error_detail FROM capture_jobs WHERE id=?",
            (receipt.job_ids[0],),
        ).fetchone()
    assert job["status"] == "retryable"
    assert "secret-value" not in job["error_detail"]


def test_partial_claim_output_completes_with_visible_diagnostic(
    worker_env, monkeypatch
) -> None:
    service, db, workspace = worker_env
    receipt = remember(text="Partially structured evidence", db=db, workspace=workspace)

    def partial(*args, **kwargs):
        return SimpleNamespace(degraded=0, partial=1, invalid_rows=1)

    monkeypatch.setattr(
        "memorymaster.bridges.atlas_llm_extractor.extract_atlas_claims_llm",
        partial,
    )
    result = run_capture_worker(service, owner="fixture", limit=1)

    assert result.completed == 1
    assert result.partial == 1
    with service.store.connect() as conn:
        job = conn.execute(
            "SELECT status, error_code FROM capture_jobs WHERE id=?",
            (receipt.job_ids[0],),
        ).fetchone()
    assert tuple(job) == ("completed", "partial_provider_output")


def test_unusable_claim_output_retries_with_stable_code(worker_env, monkeypatch) -> None:
    service, db, workspace = worker_env
    receipt = remember(text="Unusable structured evidence", db=db, workspace=workspace)

    def unusable(*args, **kwargs):
        return SimpleNamespace(degraded=1, partial=0, invalid_rows=2)

    monkeypatch.setattr(
        "memorymaster.bridges.atlas_llm_extractor.extract_atlas_claims_llm",
        unusable,
    )
    result = run_capture_worker(service, owner="fixture", limit=1)

    assert result.retryable == 1
    assert service.get_source_item_by_id(receipt.source_item["id"]) is not None
    assert service.list_evidence_items(
        source_item_id=receipt.source_item["id"], limit=10
    )
    with service.store.connect() as conn:
        job = conn.execute(
            "SELECT status, error_code FROM capture_jobs WHERE id=?",
            (receipt.job_ids[0],),
        ).fetchone()
    assert tuple(job) == ("retryable", "claim_provider_output_invalid")


@pytest.mark.parametrize(
    "producer",
    ["hermes", "whatsapp", "obsidian-clipper", "agent"],
)
def test_producer_contracts_normalize_without_fetching(producer: str) -> None:
    envelope = normalize_producer_item(
        producer,
        ProducerItem(
            external_id="fixture",
            text="Producer note token=secret-value",
            source_uri="https://example.com/note",
            content_hash="b" * 64,
        ),
    )
    assert envelope.source_kind == producer
    assert envelope.content_hash == "b" * 64
    assert "secret-value" not in (envelope.text or "")
