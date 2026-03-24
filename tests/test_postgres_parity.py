"""Postgres parity tests.

These tests validate that PostgresStore matches SQLiteStore behaviour.
When MEMORYMASTER_TEST_POSTGRES_DSN is set they run against a real database;
otherwise they are skipped.
"""
from __future__ import annotations

import json
import os

import pytest

from memorymaster.models import CLAIM_LINK_TYPES, CitationInput, ClaimLink
from memorymaster.service import MemoryService


def _pg_dsn() -> str | None:
    return os.getenv("MEMORYMASTER_TEST_POSTGRES_DSN")


def _make_pg_service() -> MemoryService:
    dsn = _pg_dsn()
    if not dsn:
        pytest.skip("MEMORYMASTER_TEST_POSTGRES_DSN is not set")
    service = MemoryService(dsn, workspace_root=".")
    service.init_db()
    _cleanup_tables(service)
    return service


def _cleanup_tables(service: MemoryService) -> None:
    """Best-effort cleanup for deterministic runs."""
    with service.store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.claim_links') AS tbl")
            links_tbl = cur.fetchone()
            if links_tbl and links_tbl["tbl"] is not None:
                cur.execute("DELETE FROM claim_links")
            cur.execute("DELETE FROM citations")
            cur.execute("SELECT to_regclass('public.claim_embeddings') AS tbl")
            emb_tbl = cur.fetchone()
            if emb_tbl and emb_tbl["tbl"] is not None:
                cur.execute("DELETE FROM claim_embeddings")
            cur.execute("DELETE FROM events")
            cur.execute("DELETE FROM claims")


def _ingest(service: MemoryService, text: str, **kwargs) -> int:
    claim = service.ingest(
        text=text,
        citations=[CitationInput(source="test", locator="loc", excerpt="exc")],
        **kwargs,
    )
    return claim.id


# ---------------------------------------------------------------------------
# Core CRUD parity
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestPostgresCoreParityCRUD:
    def test_create_and_get_claim(self):
        svc = _make_pg_service()
        claim_id = _ingest(svc, "Test claim text")

        claim = svc.store.get_claim(claim_id)
        assert claim is not None
        assert claim.text == "Test claim text"
        assert claim.status == "candidate"

    def test_idempotency_key(self):
        svc = _make_pg_service()
        c1 = svc.store.create_claim(
            "Claim A",
            [CitationInput(source="s", locator="l")],
            idempotency_key="key-1",
        )
        c2 = svc.store.create_claim(
            "Claim B different text",
            [CitationInput(source="s", locator="l")],
            idempotency_key="key-1",
        )
        assert c1.id == c2.id

    def test_get_claim_by_idempotency_key(self):
        svc = _make_pg_service()
        svc.store.create_claim(
            "Idem claim",
            [CitationInput(source="s")],
            idempotency_key="idem-pg-1",
        )
        found = svc.store.get_claim_by_idempotency_key("idem-pg-1")
        assert found is not None
        assert found.text == "Idem claim"

    def test_list_claims_with_status_filter(self):
        svc = _make_pg_service()
        _ingest(svc, "Claim alpha")
        _ingest(svc, "Claim beta")
        candidates = svc.store.list_claims(status="candidate")
        assert len(candidates) >= 2

    def test_list_claims_text_query(self):
        svc = _make_pg_service()
        _ingest(svc, "The quick brown fox")
        _ingest(svc, "A lazy dog")
        results = svc.store.list_claims(text_query="fox")
        assert any("fox" in c.text.lower() for c in results)

    def test_list_claims_scope_allowlist(self):
        svc = _make_pg_service()
        _ingest(svc, "Scoped claim")
        results = svc.store.list_claims(scope_allowlist=["project"])
        assert len(results) >= 1

    def test_list_events(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Event test claim")
        events = svc.store.list_events(claim_id=cid)
        assert len(events) >= 1
        assert events[0].event_type == "ingest"

    def test_list_citations(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Citation test")
        cites = svc.store.list_citations(cid)
        assert len(cites) >= 1
        assert cites[0].source == "test"

    def test_count_citations(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Count cites claim")
        assert svc.store.count_citations(cid) >= 1


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestPostgresParityMutations:
    def test_set_normalized_text(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Raw text here")
        svc.store.set_normalized_text(cid, "normalized version")
        claim = svc.store.get_claim(cid)
        assert claim is not None
        assert claim.normalized_text == "normalized version"

    def test_update_claim_structure(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Structure test")
        svc.store.update_claim_structure(
            cid,
            claim_type="fact",
            subject="test-subject",
            predicate="is_about",
            object_value="testing",
        )
        claim = svc.store.get_claim(cid)
        assert claim is not None
        assert claim.claim_type == "fact"
        assert claim.subject == "test-subject"

    def test_set_confidence(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Confidence test")
        svc.store.set_confidence(cid, 0.9, details="high confidence")
        claim = svc.store.get_claim(cid)
        assert claim is not None
        assert claim.confidence == pytest.approx(0.9, abs=0.01)

    def test_set_pinned(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Pin test")
        svc.store.set_pinned(cid, True, reason="important")
        claim = svc.store.get_claim(cid)
        assert claim is not None
        assert claim.pinned is True

    def test_apply_status_transition(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Transition test")
        claim = svc.store.get_claim(cid)
        assert claim is not None
        updated = svc.store.apply_status_transition(
            claim,
            to_status="confirmed",
            reason="validated",
            event_type="validator",
        )
        assert updated.status == "confirmed"

    def test_mark_superseded(self):
        svc = _make_pg_service()
        old_id = _ingest(svc, "Old claim")
        new_id = _ingest(svc, "New claim")
        # First confirm the old claim so it can be superseded
        old_claim = svc.store.get_claim(old_id)
        svc.store.apply_status_transition(
            old_claim,
            to_status="confirmed",
            reason="ok",
            event_type="validator",
        )
        svc.store.mark_superseded(old_id, new_id, "replaced by new")
        old = svc.store.get_claim(old_id)
        assert old is not None
        assert old.status == "superseded"
        assert old.replaced_by_claim_id == new_id

    def test_set_supersedes(self):
        svc = _make_pg_service()
        a = _ingest(svc, "Claim A")
        b = _ingest(svc, "Claim B")
        svc.store.set_supersedes(b, a)
        claim = svc.store.get_claim(b)
        assert claim is not None
        assert claim.supersedes_claim_id == a


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestPostgresParityRedaction:
    def test_redact_claim_payload(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Sensitive data here")
        result = svc.store.redact_claim_payload(cid, mode="redact")
        assert result["claim_rows"] == 1
        claim = svc.store.get_claim(cid)
        assert claim is not None
        assert "[REDACTED" in claim.text

    def test_erase_claim_payload(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Erase me")
        result = svc.store.redact_claim_payload(cid, mode="erase")
        assert result["claim_rows"] == 1
        claim = svc.store.get_claim(cid)
        assert claim is not None
        assert "[ERASED" in claim.text


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestPostgresParityQueries:
    def test_find_by_status(self):
        svc = _make_pg_service()
        _ingest(svc, "Status find test")
        found = svc.store.find_by_status("candidate")
        assert len(found) >= 1

    def test_find_for_decay(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Decay candidate")
        claim = svc.store.get_claim(cid)
        svc.store.apply_status_transition(
            claim,
            to_status="confirmed",
            reason="confirmed",
            event_type="validator",
        )
        found = svc.store.find_for_decay()
        assert len(found) >= 1

    def test_find_confirmed_by_tuple(self):
        svc = _make_pg_service()
        _ingest(
            svc,
            "Tuple test claim",
            subject="server",
            predicate="ip_address",
            object_value="10.0.0.1",
        )
        # Confirm the claim
        claims = svc.store.list_claims(status="candidate")
        for c in claims:
            if c.subject == "server" and c.predicate == "ip_address":
                svc.store.apply_status_transition(
                    c, to_status="confirmed", reason="ok", event_type="validator"
                )
                break
        found = svc.store.find_confirmed_by_tuple(
            subject="server", predicate="ip_address", scope="project"
        )
        assert len(found) >= 1

    def test_find_for_compaction(self):
        svc = _make_pg_service()
        # Compaction looks for old stale/superseded claims; with retain_days=0 all qualify
        found = svc.store.find_for_compaction(retain_days=0)
        # May be empty if no stale claims exist, just verify it doesn't crash
        assert isinstance(found, list)


# ---------------------------------------------------------------------------
# Event integrity
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestPostgresParityEventIntegrity:
    def test_record_event(self):
        svc = _make_pg_service()
        cid = _ingest(svc, "Event record test")
        svc.store.record_event(
            claim_id=cid,
            event_type="system",
            details="test event",
        )
        events = svc.store.list_events(claim_id=cid)
        assert any(e.event_type == "system" for e in events)

    def test_reconcile_integrity(self):
        svc = _make_pg_service()
        _ingest(svc, "Integrity check claim")
        report = svc.store.reconcile_integrity(fix=False)
        assert "issues" in report
        assert "summary" in report

    def test_delete_old_events_is_noop(self):
        svc = _make_pg_service()
        result = svc.store.delete_old_events(retain_days=30)
        assert result == 0


# ---------------------------------------------------------------------------
# Claim links
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestPostgresParityClaimLinks:
    def test_add_claim_link(self):
        svc = _make_pg_service()
        a = _ingest(svc, "Link A")
        b = _ingest(svc, "Link B")
        link = svc.store.add_claim_link(a, b, "relates_to")
        assert isinstance(link, ClaimLink)
        assert link.source_id == a
        assert link.target_id == b
        assert link.link_type == "relates_to"
        assert link.id > 0

    def test_add_all_link_types(self):
        svc = _make_pg_service()
        a = _ingest(svc, "Link types A")
        b = _ingest(svc, "Link types B")
        for lt in CLAIM_LINK_TYPES:
            link = svc.store.add_claim_link(a, b, lt)
            assert link.link_type == lt

    def test_duplicate_link_raises(self):
        svc = _make_pg_service()
        a = _ingest(svc, "Dup A")
        b = _ingest(svc, "Dup B")
        svc.store.add_claim_link(a, b, "relates_to")
        with pytest.raises(ValueError, match="Link already exists"):
            svc.store.add_claim_link(a, b, "relates_to")

    def test_self_link_raises(self):
        svc = _make_pg_service()
        a = _ingest(svc, "Self link")
        with pytest.raises(ValueError, match="must be different"):
            svc.store.add_claim_link(a, a, "relates_to")

    def test_invalid_link_type_raises(self):
        svc = _make_pg_service()
        a = _ingest(svc, "Invalid type A")
        b = _ingest(svc, "Invalid type B")
        with pytest.raises(ValueError, match="Invalid link_type"):
            svc.store.add_claim_link(a, b, "bad_type")

    def test_nonexistent_claim_raises(self):
        svc = _make_pg_service()
        a = _ingest(svc, "Exists A")
        with pytest.raises(ValueError, match="does not exist"):
            svc.store.add_claim_link(a, 999999, "relates_to")

    def test_remove_claim_link_specific_type(self):
        svc = _make_pg_service()
        a = _ingest(svc, "Rem type A")
        b = _ingest(svc, "Rem type B")
        svc.store.add_claim_link(a, b, "relates_to")
        svc.store.add_claim_link(a, b, "supports")
        removed = svc.store.remove_claim_link(a, b, "relates_to")
        assert removed == 1
        links = svc.store.get_claim_links(a)
        assert len(links) == 1
        assert links[0].link_type == "supports"

    def test_remove_claim_link_all_types(self):
        svc = _make_pg_service()
        a = _ingest(svc, "Rem all A")
        b = _ingest(svc, "Rem all B")
        svc.store.add_claim_link(a, b, "relates_to")
        svc.store.add_claim_link(a, b, "supports")
        removed = svc.store.remove_claim_link(a, b)
        assert removed == 2
        links = svc.store.get_claim_links(a)
        assert len(links) == 0

    def test_remove_nonexistent_returns_zero(self):
        svc = _make_pg_service()
        removed = svc.store.remove_claim_link(999998, 999999, "relates_to")
        assert removed == 0

    def test_get_claim_links_both_directions(self):
        svc = _make_pg_service()
        a = _ingest(svc, "Dir A")
        b = _ingest(svc, "Dir B")
        c = _ingest(svc, "Dir C")
        svc.store.add_claim_link(a, b, "relates_to")
        svc.store.add_claim_link(c, a, "supports")
        links = svc.store.get_claim_links(a)
        assert len(links) == 2

    def test_get_linked_claims_filter_by_type(self):
        svc = _make_pg_service()
        a = _ingest(svc, "Filter A")
        b = _ingest(svc, "Filter B")
        c = _ingest(svc, "Filter C")
        svc.store.add_claim_link(a, b, "relates_to")
        svc.store.add_claim_link(a, c, "supports")
        relates = svc.store.get_linked_claims(a, link_type="relates_to")
        assert len(relates) == 1
        supports = svc.store.get_linked_claims(a, link_type="supports")
        assert len(supports) == 1

    def test_get_linked_claims_no_filter(self):
        svc = _make_pg_service()
        a = _ingest(svc, "No filter A")
        b = _ingest(svc, "No filter B")
        svc.store.add_claim_link(a, b, "relates_to")
        svc.store.add_claim_link(a, b, "contradicts")
        all_links = svc.store.get_linked_claims(a)
        assert len(all_links) == 2


# ---------------------------------------------------------------------------
# Full-cycle smoke test
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_postgres_smoke_parity():
    dsn = _pg_dsn()
    if not dsn:
        pytest.skip("MEMORYMASTER_TEST_POSTGRES_DSN is not set")

    service = MemoryService(dsn, workspace_root=".")
    service.init_db()
    _cleanup_tables(service)

    service.ingest(
        text="Server IP is 10.0.0.1",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="old")],
        subject="server",
        predicate="ip_address",
        object_value="10.0.0.1",
    )
    service.ingest(
        text="Server IP is 10.0.0.2",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="new")],
        subject="server",
        predicate="ip_address",
        object_value="10.0.0.2",
    )

    result = service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.5)
    assert result["validator"]["processed"] >= 2

    rows = service.query("server ip", retrieval_mode="hybrid", limit=10, allow_sensitive=True)
    assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Init-db idempotency
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_postgres_init_db_idempotent():
    dsn = _pg_dsn()
    if not dsn:
        pytest.skip("MEMORYMASTER_TEST_POSTGRES_DSN is not set")

    service = MemoryService(dsn, workspace_root=".")
    service.init_db()
    # Second init should not fail
    service.init_db()

    cid = _ingest(service, "Idempotent init claim")
    claim = service.store.get_claim(cid)
    assert claim is not None


# ---------------------------------------------------------------------------
# Unit-level tests that work without Postgres (mock-based)
# ---------------------------------------------------------------------------


class TestPostgresStoreUnit:
    """Tests that verify PostgresStore behaviour without a real database."""

    def test_split_sql_statements_basic(self):
        from memorymaster.postgres_store import PostgresStore

        stmts = PostgresStore._split_sql_statements("SELECT 1; SELECT 2;")
        assert stmts == ["SELECT 1", "SELECT 2"]

    def test_split_sql_statements_dollar_quote(self):
        from memorymaster.postgres_store import PostgresStore

        sql = """
        CREATE FUNCTION test() RETURNS void AS $$
        BEGIN
            NULL;
        END;
        $$ LANGUAGE plpgsql;
        SELECT 1;
        """
        stmts = PostgresStore._split_sql_statements(sql)
        assert len(stmts) == 2
        assert "CREATE FUNCTION" in stmts[0]
        assert "SELECT 1" in stmts[1]

    def test_canonical_payload_none(self):
        from memorymaster.postgres_store import PostgresStore

        assert PostgresStore._canonical_payload(None) == ""

    def test_canonical_payload_string(self):
        from memorymaster.postgres_store import PostgresStore

        result = PostgresStore._canonical_payload('{"b":1,"a":2}')
        parsed = json.loads(result)
        assert list(parsed.keys()) == ["a", "b"]

    def test_canonical_payload_dict(self):
        from memorymaster.postgres_store import PostgresStore

        result = PostgresStore._canonical_payload({"z": 1, "a": 2})
        parsed = json.loads(result)
        assert list(parsed.keys()) == ["a", "z"]

    def test_vector_literal(self):
        from memorymaster.postgres_store import PostgresStore

        lit = PostgresStore._vector_literal([1.0, 2.0, 3.0])
        assert lit.startswith("[")
        assert lit.endswith("]")
        parts = lit.strip("[]").split(",")
        assert len(parts) == 3

    def test_as_iso_none(self):
        from memorymaster.postgres_store import PostgresStore

        assert PostgresStore._as_iso(None) is None

    def test_as_iso_string(self):
        from memorymaster.postgres_store import PostgresStore

        assert PostgresStore._as_iso("2025-01-01") == "2025-01-01"

    def test_as_iso_datetime(self):
        from datetime import datetime, timezone

        from memorymaster.postgres_store import PostgresStore

        dt = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = PostgresStore._as_iso(dt)
        assert "2025-01-01" in result

    def test_as_text_none(self):
        from memorymaster.postgres_store import PostgresStore

        assert PostgresStore._as_text(None) is None

    def test_as_text_value(self):
        from memorymaster.postgres_store import PostgresStore

        assert PostgresStore._as_text(42) == "42"

    def test_row_to_claim_link(self):
        from memorymaster.postgres_store import PostgresStore

        row = {
            "id": 1,
            "source_id": 10,
            "target_id": 20,
            "link_type": "relates_to",
            "created_at": "2025-01-01T00:00:00+00:00",
        }
        link = PostgresStore._row_to_claim_link(row)
        assert isinstance(link, ClaimLink)
        assert link.source_id == 10
        assert link.target_id == 20

    def test_add_claim_link_validation_invalid_type(self):
        """Validation happens before DB access, so no connection needed."""
        from memorymaster.postgres_store import PostgresStore

        store = PostgresStore.__new__(PostgresStore)
        with pytest.raises(ValueError, match="Invalid link_type"):
            store.add_claim_link(1, 2, "bad_type")

    def test_add_claim_link_validation_self_link(self):
        from memorymaster.postgres_store import PostgresStore

        store = PostgresStore.__new__(PostgresStore)
        with pytest.raises(ValueError, match="must be different"):
            store.add_claim_link(5, 5, "relates_to")
