"""Adversarial tests for tenant-local claim identity and tuple semantics."""
from __future__ import annotations

import sqlite3

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import validator


def _service(db_path, tenant_id: str) -> MemoryService:
    return MemoryService(db_path, workspace_root=db_path.parent, tenant_id=tenant_id)


def _ingest(
    service: MemoryService,
    *,
    text: str,
    idempotency_key: str,
    object_value: str,
):
    return service.ingest(
        text=text,
        citations=[CitationInput(source="tenant-test", locator="fixture")],
        idempotency_key=idempotency_key,
        subject="shared-subject",
        predicate="uses",
        object_value=object_value,
        scope="project:shared",
        source_agent="tenant-identity-test",
    )


def test_same_idempotency_and_human_id_can_coexist_across_tenants(tmp_path) -> None:
    db_path = tmp_path / "tenant-identities.db"
    tenant_a = _service(db_path, "tenant-a")
    tenant_a.init_db()
    tenant_b = _service(db_path, "tenant-b")

    claim_a = _ingest(
        tenant_a,
        text="The shared service uses PostgreSQL.",
        idempotency_key="shared-import-key",
        object_value="postgres-a",
    )
    claim_b = _ingest(
        tenant_b,
        text="The shared service uses PostgreSQL.",
        idempotency_key="shared-import-key",
        object_value="postgres-b",
    )

    assert claim_a.id != claim_b.id
    assert claim_a.human_id == claim_b.human_id
    assert claim_a.tenant_id == "tenant-a"
    assert claim_b.tenant_id == "tenant-b"


def test_identity_lookups_are_tenant_qualified(tmp_path) -> None:
    db_path = tmp_path / "tenant-lookups.db"
    tenant_a = _service(db_path, "tenant-a")
    tenant_a.init_db()
    tenant_b = _service(db_path, "tenant-b")
    claim_a = _ingest(
        tenant_a,
        text="Shared lookup identity.",
        idempotency_key="same-key",
        object_value="a",
    )
    claim_b = _ingest(
        tenant_b,
        text="Shared lookup identity.",
        idempotency_key="same-key",
        object_value="b",
    )

    by_key_a = tenant_a.store.get_claim_by_idempotency_key(
        "same-key", tenant_id="tenant-a"
    )
    by_key_b = tenant_b.store.get_claim_by_idempotency_key(
        "same-key", tenant_id="tenant-b"
    )
    by_human_a = tenant_a.store.get_claim_by_human_id(
        claim_a.human_id, tenant_id="tenant-a"
    )
    by_human_b = tenant_b.store.get_claim_by_human_id(
        claim_b.human_id, tenant_id="tenant-b"
    )

    assert by_key_a and by_key_a.id == claim_a.id
    assert by_key_b and by_key_b.id == claim_b.id
    assert by_human_a and by_human_a.id == claim_a.id
    assert by_human_b and by_human_b.id == claim_b.id
    assert (
        tenant_a.store.resolve_claim_id(
            claim_a.human_id,
            tenant_id="tenant-a",
        )
        == claim_a.id
    )
    assert (
        tenant_b.store.resolve_claim_id(
            claim_b.human_id,
            tenant_id="tenant-b",
        )
        == claim_b.id
    )


def test_same_confirmed_tuple_can_coexist_across_tenants(tmp_path) -> None:
    db_path = tmp_path / "tenant-tuples.db"
    tenant_a = _service(db_path, "tenant-a")
    tenant_a.init_db()
    tenant_b = _service(db_path, "tenant-b")
    claim_a = _ingest(
        tenant_a,
        text="Tenant A tuple.",
        idempotency_key="tuple-a",
        object_value="a",
    )
    claim_b = _ingest(
        tenant_b,
        text="Tenant B tuple.",
        idempotency_key="tuple-b",
        object_value="b",
    )

    transition_claim(
        tenant_a.store,
        claim_a.id,
        "confirmed",
        reason="tenant-a-confirm",
        event_type="validator",
    )
    transition_claim(
        tenant_b.store,
        claim_b.id,
        "confirmed",
        reason="tenant-b-confirm",
        event_type="validator",
    )

    with tenant_a.store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="only one confirmed claim"):
            conn.execute(
                "UPDATE claims SET tenant_id = ? WHERE id = ?",
                ("tenant-a", claim_b.id),
            )

    with tenant_a.store.connect() as conn:
        rows = conn.execute(
            "SELECT tenant_id, status FROM claims ORDER BY tenant_id"
        ).fetchall()
    assert [(row["tenant_id"], row["status"]) for row in rows] == [
        ("tenant-a", "confirmed"),
        ("tenant-b", "confirmed"),
    ]


def test_duplicate_identity_and_tuple_still_fail_within_tenant(tmp_path) -> None:
    db_path = tmp_path / "same-tenant.db"
    service = _service(db_path, "tenant-a")
    service.init_db()
    first = _ingest(
        service,
        text="First identity.",
        idempotency_key="tenant-key",
        object_value="first",
    )
    duplicate = _ingest(
        service,
        text="Different payload with the same key.",
        idempotency_key="tenant-key",
        object_value="duplicate",
    )
    second_tuple = _ingest(
        service,
        text="Second tuple.",
        idempotency_key="tuple-second",
        object_value="second",
    )

    assert duplicate.id == first.id
    transition_claim(
        service.store,
        first.id,
        "confirmed",
        reason="first-confirm",
        event_type="validator",
    )
    with pytest.raises(sqlite3.IntegrityError, match="only one confirmed claim"):
        transition_claim(
            service.store,
            second_tuple.id,
            "confirmed",
            reason="duplicate-confirm",
            event_type="validator",
        )


def test_validator_does_not_conflict_with_another_tenants_tuple(tmp_path) -> None:
    db_path = tmp_path / "tenant-validator.db"
    tenant_a = _service(db_path, "tenant-a")
    tenant_a.init_db()
    tenant_b = _service(db_path, "tenant-b")
    claim_a = _ingest(
        tenant_a,
        text="Tenant A confirmed truth.",
        idempotency_key="validator-a",
        object_value="a",
    )
    claim_b = _ingest(
        tenant_b,
        text="Tenant B independent truth.",
        idempotency_key="validator-b",
        object_value="b",
    )
    transition_claim(
        tenant_a.store,
        claim_a.id,
        "confirmed",
        reason="fixture",
        event_type="validator",
    )

    validator.run(tenant_b.store, min_citations=1, min_score=0.0)

    refreshed = tenant_b.store.get_claim(claim_b.id)
    assert refreshed is not None
    assert refreshed.status == "confirmed"
