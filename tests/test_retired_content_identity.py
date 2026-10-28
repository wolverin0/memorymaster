"""Explicit retirement follows content across independently chosen ingest keys."""

import pytest

from memorymaster.core.intake_policy import reset_intake_state
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


TEXT = "The project preserves independently cited architecture decisions."


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(tmp_path / "aliases.json"))
    monkeypatch.setattr(MemoryService, "_init_qdrant", staticmethod(lambda: None))
    monkeypatch.setattr("memorymaster.core.webhook.fire_webhook", lambda *args, **kwargs: None)
    monkeypatch.setattr("memorymaster.knowledge.entity_ingest.resolve_claim_entities", lambda *args, **kwargs: 0)
    reset_intake_state()
    result = MemoryService(tmp_path / "claims.db", workspace_root=tmp_path, tenant_id="tenant-a")
    result.init_db()
    return result


def _ingest(service, *, key="dream-original", text=TEXT, source="dream-worker", visibility="public",
            scope="project:test", receipt=None, citation="original"):
    return service.ingest(
        text, [CitationInput(citation, locator=f"{citation}:1")], idempotency_key=key,
        source_agent=source, visibility=visibility, scope=scope, outcome_receipt=receipt,
    )


@pytest.mark.parametrize("reason", ["friendly_forget_claim", "ao7_rollback"])
@pytest.mark.parametrize("key", [None, "different-explicit-key"])
def test_retired_content_blocks_new_key_and_hash_intake(service, reason, key):
    original = _ingest(service)
    retired = service.store.apply_status_transition(
        original, to_status="archived", reason=reason, event_type="transition",
    )
    service.store.record_event(claim_id=original.id, event_type="audit", details="later_unrelated_audit")
    receipt = {"operation_id": "later-intake"}
    match = _ingest(service, key=key, text="  " + TEXT.upper() + "  ", citation="replacement", receipt=receipt)
    assert (match.id, match.status, match.version) == (retired.id, "archived", retired.version)
    assert match.citations == original.citations
    assert receipt["status"] == "blocked" and not receipt["created"] and not receipt["owned"]
    assert len(service.list_claims(status="archived", limit=100)) == 1
    assert service.list_claims(status="candidate", limit=100) == []


@pytest.mark.parametrize("key", [None, "dream-original"])
def test_latest_ordinary_archive_does_not_block_content_relearning(service, key):
    claim = _ingest(service)
    retired = service.store.apply_status_transition(claim, to_status="archived", reason="ao7_rollback", event_type="transition")
    relearned = service.store.apply_status_transition(
        retired, to_status="candidate", reason="operator_relearn", event_type="transition",
    )
    service.store.apply_status_transition(
        relearned, to_status="archived", reason="ordinary_cleanup", event_type="transition",
    )
    receipt = {"operation_id": "governed-later-intake"}
    result = _ingest(service, key=key, receipt=receipt)
    assert result.status == "candidate" and receipt["status"] != "blocked"
    # Identical content is matched whatever key the original used (T-1865 F14), so an
    # ordinarily archived original is revived for both keyed and unkeyed re-ingest.
    assert result.id == claim.id and not receipt["created"]


@pytest.mark.parametrize("partition", ["tenant", "scope", "visibility", "private_source"])
def test_retired_content_stays_within_canonical_identity_partition(service, tmp_path, partition):
    base_visibility = "private" if partition == "private_source" else "public"
    claim = _ingest(service, visibility=base_visibility)
    service.store.apply_status_transition(claim, to_status="archived", reason="ao7_rollback", event_type="transition")
    target = MemoryService(service.store.db_path, workspace_root=tmp_path,
                           tenant_id="tenant-b" if partition == "tenant" else "tenant-a")
    receipt = {"operation_id": "separate-partition"}
    result = _ingest(
        target, key=None, receipt=receipt,
        scope="project:other" if partition == "scope" else "project:test",
        visibility="private" if partition in {"visibility", "private_source"} else "public",
        source="other-writer" if partition == "private_source" else "dream-worker",
    )
    assert result.id != claim.id and result.status == "candidate"
    assert receipt["created"] and receipt["owned"]
    assert service.store.get_claim(claim.id).status == "archived"


@pytest.mark.parametrize("key", [None, "different-direct-key"])
def test_atomic_store_content_guard_catches_archive_after_facade_preflight(service, monkeypatch, key):
    original = _ingest(service)
    check = service.store._check_idempotency
    # Simulate an archive committed by a concurrent writer after the optimistic
    # store lookup, immediately before final content validation and insertion.
    def archive_after_optimistic_lookup(*args, **kwargs):
        result = check(*args, **kwargs)
        service.store.apply_status_transition(
            original, to_status="archived", reason="ao7_rollback", event_type="transition",
        )
        return result

    monkeypatch.setattr(service.store, "_check_idempotency", archive_after_optimistic_lookup)
    # The optimistic content lookups must miss for this race to exist: the concurrent
    # archive lands after them, which is exactly the window the atomic guard closes.
    monkeypatch.setattr(service.store, "get_claim_by_content_fingerprint", lambda *_a, **_k: None)
    receipt = {"operation_id": "racing-intake"}
    result = _ingest(service, key=key, citation="replacement", receipt=receipt)
    assert result.id == original.id and result.status == "archived"
    assert result.citations == original.citations
    assert receipt["status"] == "blocked" and not receipt["created"] and not receipt["owned"]
    assert service.list_claims(status="candidate", limit=100) == []
