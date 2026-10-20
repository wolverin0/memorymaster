"""Disposable ownership receipts and cross-path rollback retirement guards."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from memorymaster.core.models import CitationInput, Claim
from memorymaster.core.intake_policy import reset_intake_state
from memorymaster.core.service import MemoryService


@pytest.fixture
def service(tmp_path, monkeypatch):
    reset_intake_state()
    monkeypatch.setattr(MemoryService, "_init_qdrant", staticmethod(lambda: None))
    monkeypatch.setattr("memorymaster.core.webhook.fire_webhook", lambda *args, **kwargs: None)
    monkeypatch.setattr("memorymaster.knowledge.entity_ingest.resolve_claim_entities", lambda *args, **kwargs: 0)
    instance = MemoryService(tmp_path / "rollback.db", workspace_root=tmp_path)
    instance.init_db()
    return instance


def _ingest(service, *, receipt=None, source="original", key="stable-operation"):
    return service.ingest(
        "The project preserves independently supported architecture evidence.",
        [CitationInput(source, locator=f"{source}:1")], source_agent="dream-worker",
        scope="project:test", idempotency_key=key, outcome_receipt=receipt,
        intake_batch_id="cohort-fixture", intake_batch_max=200,
    )


def test_creation_receipt_recovers_owned_operation_after_crash_replay(service, tmp_path):
    first = {"operation_id": "application-1"}
    claim = _ingest(service, receipt=first)
    assert isinstance(claim, Claim)
    assert first["created"] and first["owned"] and first["status"] == "created"
    restarted = MemoryService(service.store.db_path, workspace_root=tmp_path)
    replay = {"operation_id": "application-1"}
    assert _ingest(restarted, receipt=replay, source="different").id == claim.id
    assert replay["status"] == "reused" and not replay["created"] and replay["owned"]
    assert replay["intake_batch_id"] == "cohort-fixture"
    other = {"operation_id": "application-2"}
    _ingest(restarted, receipt=other)
    assert not other["created"] and not other["owned"]
    assert restarted.store.get_claim(claim.id).citations == claim.citations


def test_receipt_never_owns_preexisting_claim_without_creation_operation(service):
    claim = _ingest(service)
    receipt = {"operation_id": "application-1", "created": True, "owned": True}
    assert _ingest(service, receipt=receipt).id == claim.id
    assert receipt["status"] == "reused"
    assert not receipt["created"] and not receipt["owned"]
    assert receipt["creation_operation_id"] is None


@pytest.mark.parametrize("with_receipt", [False, True])
def test_live_dedup_race_skips_post_ingest_writes_on_existing_winner(service, monkeypatch, with_receipt):
    claim = _ingest(service)
    monkeypatch.setattr(service.store, "get_claim_by_idempotency_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(service.store, "_check_idempotency", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "memorymaster.govern.ingest_governance.apply_post_ingest_governance",
        lambda *args, **kwargs: pytest.fail("race winner treated as new intake"),
    )
    receipt = {"operation_id": "later-backfill"} if with_receipt else None
    reused = _ingest(service, receipt=receipt, source="replacement")
    assert reused.id == claim.id and reused.citations == claim.citations
    assert len(service.store.list_events(claim.id)) == 1
    if receipt is not None:
        assert receipt["status"] == "reused" and not receipt["created"] and not receipt["owned"]


def test_concurrent_dedup_receipts_name_only_the_atomic_creation_owner(service, tmp_path, monkeypatch):
    other = MemoryService(service.store.db_path, workspace_root=tmp_path)
    barrier = Barrier(2)
    for store in (service.store, other.store):
        original = store._check_idempotency

        def synchronized_check(*args, _original=original, **kwargs):
            result = _original(*args, **kwargs)
            barrier.wait(timeout=20)
            return result

        monkeypatch.setattr(store, "_check_idempotency", synchronized_check)
    receipts = [{"operation_id": "application-a"}, {"operation_id": "application-b"}]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_ingest, instance, receipt=receipt, source=source)
                   for instance, receipt, source in zip((service, other), receipts, ("alpha", "beta"))]
        claims = [future.result(timeout=30) for future in futures]
    assert claims[0].id == claims[1].id
    assert sum(receipt["created"] for receipt in receipts) == 1
    assert sum(receipt["owned"] for receipt in receipts) == 1
    winner = next(index for index, receipt in enumerate(receipts) if receipt["created"])
    assert service.store.get_claim(claims[0].id).citations[0].source == ("alpha", "beta")[winner]
    assert {receipt["creation_operation_id"] for receipt in receipts} == {receipts[winner]["operation_id"]}


@pytest.mark.parametrize("reason", ["friendly_forget_claim", "ao7_rollback"])
@pytest.mark.parametrize("key", [None, "explicit-key"])
@pytest.mark.parametrize("with_receipt", [False, True])
def test_retired_dedup_race_cannot_revive_or_replace_provenance(service, monkeypatch, reason, key, with_receipt):
    claim = _ingest(service, key=key)
    service.store.record_event(claim_id=claim.id, event_type="audit", details="original_source_review", payload={"source": "original"})
    retired = service.store.apply_status_transition(claim, to_status="archived", reason=reason, event_type="transition")
    # Force both facade lookups and the store's optimistic lookup to miss;
    # the unique constraint must recover the actual concurrent winner.
    monkeypatch.setattr(service.store, "get_claim_by_idempotency_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(service.store, "_check_idempotency", lambda *args, **kwargs: None)
    receipt = {"operation_id": "later-backfill"} if with_receipt else None
    recovered = _ingest(service, receipt=receipt, source="replacement", key=key)
    assert (recovered.id, recovered.status, recovered.version) == (retired.id, "archived", retired.version)
    assert recovered.citations == claim.citations
    assert [event.details for event in service.store.list_events(claim.id)] == [reason, "original_source_review", "claim_ingested"]
    if receipt is not None:
        assert receipt["status"] == "blocked" and not receipt["created"] and not receipt["owned"]


@pytest.mark.parametrize("reason", ["friendly_forget_claim", "ao7_rollback"])
def test_store_retirement_guard_survives_service_check_race(service, monkeypatch, reason):
    claim = _ingest(service)
    stale_archived = service.store.apply_status_transition(
        claim, to_status="archived", reason="ordinary_cleanup", event_type="transition",
    )
    original = service.store.latest_archive_reason

    def retire_after_check(claim_id):
        result = original(claim_id)
        service.store.apply_status_transition(
            stale_archived, to_status="archived", reason=reason, event_type="transition",
        )
        return result

    monkeypatch.setattr(service.store, "latest_archive_reason", retire_after_check)
    recovered = _ingest(service)
    assert recovered.status == service.store.get_claim(claim.id).status == "archived"
    assert not any(event.details.startswith("reingest_revival:") for event in service.store.list_events(claim.id))
