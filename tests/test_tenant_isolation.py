"""Tests for multi-tenant isolation (tenant_id column and filtering)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


def _make_db() -> str:
    return str(Path(tempfile.mkdtemp()) / "test_tenant.db")


def _make_service(db: str, tenant_id: str | None = None) -> MemoryService:
    svc = MemoryService(db, workspace_root=Path.cwd(), tenant_id=tenant_id)
    svc.init_db()
    return svc


def _cite() -> list[CitationInput]:
    return [CitationInput(source="test")]


class TestTenantIsolation:
    """Verify that tenant_id provides proper data isolation."""

    def test_no_tenant_backward_compat(self):
        """Claims ingested without tenant_id get tenant_id=None."""
        db = _make_db()
        svc = _make_service(db)
        claim = svc.ingest("hello world", _cite())
        assert claim.tenant_id is None

    def test_tenant_id_stored_on_claim(self):
        """Claims ingested with a tenant get the correct tenant_id."""
        db = _make_db()
        svc = _make_service(db, tenant_id="acme")
        claim = svc.ingest("acme claim", _cite())
        assert claim.tenant_id == "acme"

    def test_tenant_id_whitespace_normalized(self):
        """Whitespace-only tenant_id is treated as None."""
        db = _make_db()
        svc = _make_service(db, tenant_id="  ")
        claim = svc.ingest("no real tenant", _cite())
        assert claim.tenant_id is None

    def test_list_claims_filtered_by_tenant(self):
        """list_claims only returns claims for the active tenant."""
        db = _make_db()
        svc_a = _make_service(db, tenant_id="a")
        svc_b = _make_service(db, tenant_id="b")
        svc_none = _make_service(db)

        svc_a.ingest("claim A1", _cite())
        svc_a.ingest("claim A2", _cite())
        svc_b.ingest("claim B1", _cite())
        svc_none.ingest("claim global", _cite())

        # Tenant A sees only its own claims
        claims_a = svc_a.list_claims()
        assert len(claims_a) == 2
        assert all(c.tenant_id == "a" for c in claims_a)

        # Tenant B sees only its own claims
        claims_b = svc_b.list_claims()
        assert len(claims_b) == 1
        assert claims_b[0].tenant_id == "b"

        # No-tenant service sees all claims
        claims_all = svc_none.list_claims()
        assert len(claims_all) == 4

    def test_query_filtered_by_tenant(self):
        """query() respects tenant isolation."""
        db = _make_db()
        svc_a = _make_service(db, tenant_id="a")
        svc_b = _make_service(db, tenant_id="b")

        svc_a.ingest("shared topic alpha", _cite())
        svc_b.ingest("shared topic alpha", _cite())

        results_a = svc_a.query(
            "alpha",
            retrieval_mode="legacy",
            include_candidates=True,
        )
        assert len(results_a) == 1
        assert results_a[0].tenant_id == "a"

        results_b = svc_b.query(
            "alpha",
            retrieval_mode="legacy",
            include_candidates=True,
        )
        assert len(results_b) == 1
        assert results_b[0].tenant_id == "b"

    def test_pin_blocked_across_tenants(self):
        """Pinning a claim belonging to another tenant raises ValueError."""
        db = _make_db()
        svc_a = _make_service(db, tenant_id="a")
        svc_b = _make_service(db, tenant_id="b")

        claim_b = svc_b.ingest("b's claim", _cite())

        with pytest.raises(ValueError):
            svc_a.pin(claim_b.id)

    def test_pin_allowed_same_tenant(self):
        """Pinning a claim in the same tenant works."""
        db = _make_db()
        svc_a = _make_service(db, tenant_id="a")
        claim = svc_a.ingest("a's claim", _cite())
        result = svc_a.pin(claim.id)
        assert result.pinned is True

    def test_pin_allowed_no_tenant(self):
        """A service without tenant_id can pin any claim."""
        db = _make_db()
        svc_a = _make_service(db, tenant_id="a")
        svc_none = _make_service(db)

        claim = svc_a.ingest("a's claim", _cite())
        result = svc_none.pin(claim.id)
        assert result.pinned is True

    def test_redact_blocked_across_tenants(self):
        """Redacting a claim belonging to another tenant raises ValueError."""
        db = _make_db()
        svc_a = _make_service(db, tenant_id="a")
        svc_b = _make_service(db, tenant_id="b")

        claim_b = svc_b.ingest("b's secret", _cite())

        with pytest.raises(ValueError):
            svc_a.redact_claim_payload(claim_b.id)

    def test_storage_list_claims_tenant_param(self):
        """Storage-level list_claims tenant_id parameter filters correctly."""
        db = _make_db()
        svc = _make_service(db)
        store = svc.store

        store.create_claim("t1 claim", _cite(), tenant_id="t1")
        store.create_claim("t2 claim", _cite(), tenant_id="t2")
        store.create_claim("no tenant", _cite())

        t1_claims = store.list_claims(tenant_id="t1")
        assert len(t1_claims) == 1
        assert t1_claims[0].tenant_id == "t1"

        t2_claims = store.list_claims(tenant_id="t2")
        assert len(t2_claims) == 1

        all_claims = store.list_claims()
        assert len(all_claims) == 3

    def test_schema_migration_idempotent(self):
        """Calling init_db multiple times does not fail (column already exists)."""
        db = _make_db()
        svc = _make_service(db)
        svc.init_db()
        svc.init_db()  # Should not raise
