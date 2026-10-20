"""Public forget intent survives matching ingestion and later audit traffic."""

import pytest

from memorymaster.core.models import CitationInput, Claim
from memorymaster.core.service import MemoryService
from memorymaster.public.v1 import forget


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(MemoryService, "_init_qdrant", staticmethod(lambda: None))
    monkeypatch.setattr("memorymaster.core.webhook.fire_webhook", lambda *args, **kwargs: None)
    monkeypatch.setattr("memorymaster.knowledge.entity_ingest.resolve_claim_entities", lambda *args, **kwargs: 0)
    instance = MemoryService(tmp_path / "claims.db", workspace_root=tmp_path)
    instance.init_db()
    return instance


@pytest.mark.parametrize("key", [None, "explicit-forget-key"])
def test_friendly_forget_blocks_idempotency_and_hash_revival(service, tmp_path, key, monkeypatch):
    kwargs = {"idempotency_key": key, "scope": "project:test", "source_agent": "dream-worker"}
    text = "The project retains cited architecture decisions."
    claim = service.ingest(text, [CitationInput("original", locator="source:1")], **kwargs)
    forget(claim_id=claim.id, apply=True, db=service.store.db_path, workspace=tmp_path)
    service.store.record_event(
        claim_id=claim.id, event_type="audit", from_status="archived", to_status="archived",
        details="unrelated_bookkeeping",
    )
    # Prove refusal at the facade, rather than relying only on the store guard.
    monkeypatch.setattr(service.store, "apply_status_transition", lambda *args, **kwargs: pytest.fail("retired revival attempted"))
    receipt = {"operation_id": "later-backfill"}
    result = service.ingest(text, [CitationInput("replacement")], outcome_receipt=receipt, **kwargs)
    assert isinstance(result, Claim)
    assert (result.id, result.status) == (claim.id, "archived")
    assert result.citations == claim.citations
    assert receipt["status"] == "blocked"
    assert not receipt["created"] and not receipt["owned"]


@pytest.mark.parametrize("reason", ["friendly_forget_claim", "ao7_rollback"])
def test_latest_ordinary_archive_allows_revival_after_explicit_relearning(service, reason):
    text = "The project uses isolated disposable lifecycle fixtures."
    kwargs = {"source_agent": "dream-worker", "scope": "project:test"}
    claim = service.ingest(text, [CitationInput("original")], **kwargs)
    retired = service.store.apply_status_transition(claim, to_status="archived", reason=reason, event_type="transition")
    relearned = service.store.apply_status_transition(
        retired, to_status="candidate", reason="operator_relearn", event_type="transition",
    )
    service.store.apply_status_transition(
        relearned, to_status="archived", reason="archive_by_source:dream-worker", event_type="transition",
    )
    revived = service.ingest(text, [CitationInput("later")], **kwargs)
    assert (revived.id, revived.status) == (claim.id, "candidate")
    assert service.store.latest_archive_reason(claim.id) == "archive_by_source:dream-worker"
