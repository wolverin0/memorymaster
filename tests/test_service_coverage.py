"""Tests for memorymaster.core.service — coverage gaps."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from memorymaster.core import llm_budget
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


@pytest.fixture
def svc(tmp_path):
    """Create a real MemoryService with SQLite in tmp_path."""
    db = str(tmp_path / "test.db")
    s = MemoryService(db_target=db, workspace_root=tmp_path)
    s.init_db()
    return s


def _ingest(svc, text="Test claim", source="test.py"):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source=source)],
    )


class TestInitQdrant:
    def test_no_env_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QDRANT_URL", None)
            assert MemoryService._init_qdrant() is None

    def test_with_env_but_unreachable(self):
        with patch.dict(os.environ, {"QDRANT_URL": "http://localhost:1"}):
            # Should fail gracefully and return None
            result = MemoryService._init_qdrant()
            assert result is None

    def test_with_env_and_working_backend(self):
        mock_backend = MagicMock()
        with patch.dict(os.environ, {"QDRANT_URL": "http://localhost:6333"}):
            with patch("memorymaster.recall.qdrant_backend.QdrantBackend", return_value=mock_backend):
                result = MemoryService._init_qdrant()
                assert result is mock_backend
                mock_backend.ensure_collection.assert_called_once()


class TestQdrantSync:
    def _make_svc_with_qdrant(self, tmp_path):
        svc = MemoryService(db_target=str(tmp_path / "t.db"), workspace_root=tmp_path)
        svc.init_db()
        svc.qdrant = MagicMock()
        return svc

    def test_sync_upserts_for_confirmed(self, tmp_path):
        svc = self._make_svc_with_qdrant(tmp_path)
        _ingest(svc, "Test")
        # qdrant.upsert_claim should have been called
        svc.qdrant.upsert_claim.assert_called()

    def test_sync_deletes_for_archived(self, tmp_path):
        svc = self._make_svc_with_qdrant(tmp_path)
        fake_claim = MagicMock()
        fake_claim.id = 1
        fake_claim.status = "archived"
        svc._qdrant_sync(fake_claim)
        svc.qdrant.delete_claim.assert_called_once_with(1)

    def test_sync_handles_exception(self, tmp_path):
        svc = self._make_svc_with_qdrant(tmp_path)
        svc.qdrant.upsert_claim.side_effect = Exception("boom")
        fake_claim = MagicMock()
        fake_claim.id = 1
        fake_claim.status = "confirmed"
        # Should not raise
        svc._qdrant_sync(fake_claim)


class TestQdrantPostCycleSync:
    def test_post_cycle_upserts(self, tmp_path):
        svc = MemoryService(db_target=str(tmp_path / "t.db"), workspace_root=tmp_path)
        svc.init_db()
        svc.qdrant = MagicMock()
        _ingest(svc, "Claim for cycle")
        svc.run_cycle()
        # qdrant.upsert_claim should have been called during post-cycle
        assert svc.qdrant.upsert_claim.call_count >= 1

    def test_post_cycle_handles_exception(self, tmp_path):
        svc = MemoryService(db_target=str(tmp_path / "t.db"), workspace_root=tmp_path)
        svc.init_db()
        svc.qdrant = MagicMock()
        svc.qdrant.upsert_claim.side_effect = Exception("qdrant down")
        _ingest(svc, "Claim")
        # Should not raise
        svc._qdrant_post_cycle_sync()


class TestNormalizeScopeAllowlist:
    def test_none(self):
        assert MemoryService._normalize_scope_allowlist(None) is None

    def test_empty_list(self):
        assert MemoryService._normalize_scope_allowlist([]) is None

    def test_whitespace_only(self):
        assert MemoryService._normalize_scope_allowlist(["  ", ""]) is None

    def test_dedup(self):
        result = MemoryService._normalize_scope_allowlist(["a", "b", "a"])
        assert result == ["a", "b"]

    def test_strips(self):
        result = MemoryService._normalize_scope_allowlist(["  project  ", "global"])
        assert result == ["project", "global"]


class TestQueryForContext(object):
    def test_returns_context_result(self, svc):
        _ingest(svc, "Python uses indentation")
        result = svc.query_for_context(query="Python", token_budget=2000)
        assert hasattr(result, "output")
        assert hasattr(result, "claims_considered")
        assert hasattr(result, "tokens_used")

    def test_empty_db(self, svc):
        result = svc.query_for_context(query="anything")
        assert result.claims_considered == 0


class TestIngestEdgeCases:
    def test_empty_text_raises(self, svc):
        with pytest.raises(ValueError, match="empty"):
            svc.ingest(text="", citations=[CitationInput(source="x")])

    def test_whitespace_only_raises(self, svc):
        with pytest.raises(ValueError, match="empty"):
            svc.ingest(text="   ", citations=[CitationInput(source="x")])

    def test_no_citations_auto_generates(self, svc):
        claim = svc.ingest(text="Valid text", citations=[])
        assert claim.id is not None
        assert len(claim.citations) == 1
        assert claim.citations[0].source == "mcp-session"

    def test_idempotency_key_dedup(self, svc):
        c1 = svc.ingest(text="First", citations=[CitationInput(source="x")], idempotency_key="key1")
        c2 = svc.ingest(text="Second", citations=[CitationInput(source="x")], idempotency_key="key1")
        assert c1.id == c2.id  # Same claim returned

    def test_content_hash_dedup_without_explicit_key(self, svc):
        first = svc.ingest(
            text="The planner cache is safe to reuse",
            citations=[CitationInput(source="first")],
            scope="project:memorymaster",
        )
        second = svc.ingest(
            text="  the planner cache is safe to reuse  ",
            citations=[CitationInput(source="second")],
            scope="project:memorymaster",
        )

        assert first.id == second.id, "content-hash dedupe prevents citation-only reimports from duplicating claims"
        assert first.idempotency_key.startswith("hash-")

    def test_sensitive_ingest_redacts_and_records_encrypted_payload(self, svc):
        fernet = pytest.importorskip("cryptography.fernet").Fernet
        secret = "sk_test_1234567890abcdef"

        with patch.dict(os.environ, {"MEMORYMASTER_ENCRYPTION_KEY": fernet.generate_key().decode("utf-8")}):
            claim = svc.ingest(
                text=f"Stripe token is {secret}",
                object_value="deploy password=SecretValue99",
                subject=f"credential {secret}",
                predicate="documents",
                citations=[CitationInput(source="incident", excerpt=f"token={secret}")],
            )

        assert secret not in claim.text, "sensitive ingest must redact before storing retrievable claim text"
        assert "[REDACTED:stripe_key]" in claim.text
        assert claim.object_value and "[REDACTED:password_assignment]" in claim.object_value
        assert claim.subject and "[REDACTED:stripe_key]" in claim.subject
        assert claim.citations and secret not in (claim.citations[0].excerpt or "")

        policy_events = svc.list_events(claim_id=claim.id, event_type="policy_decision")
        encrypted_events = [event for event in policy_events if event.details == "sensitive_payload_encrypted"]
        assert encrypted_events, "encrypted audit payload preserves forensic recovery without leaking into claim rows"
        payload = json.loads(encrypted_events[0].payload_json or "{}")
        assert payload["ciphertext_b64"]
        assert secret not in encrypted_events[0].payload_json


class TestQueryRowsCoverage:
    def test_legacy_and_hybrid_paths_return_ranked_rows(self, svc):
        _ingest(svc, "MemoryMaster query_rows ranks the service cache branch")

        legacy_rows = svc.query_rows("query_rows service", include_candidates=True, retrieval_mode="legacy")
        hybrid_rows = svc.query_rows(
            "query_rows service",
            include_candidates=True,
            retrieval_mode="hybrid",
            vector_hook=lambda _query, claims: {claim.id: 0.25 for claim in claims},
        )

        assert legacy_rows, "legacy mode remains the fast compatibility path for MCP callers"
        assert hybrid_rows, "hybrid mode must keep returning the richer ranked-row contract"
        assert {"claim", "annotation", "score", "lexical_score", "vector_score"} <= set(hybrid_rows[0])
        assert hybrid_rows[0]["vector_score"] == pytest.approx(0.25)

    def test_hybrid_cache_hit_rehydrates_rows_without_reranking(self, svc):
        claim = _ingest(svc, "Cached hybrid query rows should be reusable")

        def vector_hook(_query, claims):
            return {row.id: 0.5 for row in claims}

        with patch.dict(os.environ, {"MEMORYMASTER_QUERY_CACHE": "1"}):
            first = svc.query_rows(
                "cached hybrid",
                include_candidates=True,
                retrieval_mode="hybrid",
                vector_hook=vector_hook,
            )
            with patch("memorymaster.core.service.rank_claim_rows", side_effect=AssertionError("cache miss reranked")):
                second = svc.query_rows(
                    "cached hybrid",
                    include_candidates=True,
                    retrieval_mode="hybrid",
                    vector_hook=vector_hook,
                )

        assert [row["claim"].id for row in first] == [claim.id]
        assert [row["claim"].id for row in second] == [claim.id], "cache hits must rehydrate live claims by id"


class TestRunCycleBudgetAbort:
    def test_run_cycle_returns_budget_abort_instead_of_raising(self, svc):
        with patch("memorymaster.core.service.extractor.run", side_effect=llm_budget.LLMBudgetExceeded("calls_exhausted")):
            result = svc.run_cycle()

        assert result["budget"]["aborted"] is True, "budget caps must stop steward work without crashing callers"
        assert result["budget"]["aborted_reason"] == "calls_exhausted"
        assert "validator" not in result


class TestPinErrors:
    def test_pin_nonexistent_raises(self, svc):
        with pytest.raises(ValueError, match="does not exist"):
            svc.pin(claim_id=99999)


class TestRedactErrors:
    def test_redact_zero_id_raises(self, svc):
        with pytest.raises(ValueError, match="positive"):
            svc.redact_claim_payload(claim_id=0)

    def test_redact_negative_id_raises(self, svc):
        with pytest.raises(ValueError, match="positive"):
            svc.redact_claim_payload(claim_id=-1)


class TestRunCycleRecomputesTiers:
    """run_cycle must recompute tiers every cycle (governance regression).

    WHY: recompute_tiers was never wired into run_cycle, so on the live DB
    tiers drifted — heavy-use claims stayed 'working' and 'peripheral' never
    populated. This asserts the cycle now self-maintains tiers.
    """

    def test_run_cycle_includes_recompute_tiers_phase(self, tmp_path):
        svc = MemoryService(db_target=str(tmp_path / "t.db"), workspace_root=tmp_path)
        svc.init_db()
        _ingest(svc, "A claim that should be tiered by the cycle")
        result = svc.run_cycle()
        assert "recompute_tiers" in result
        tiers = result["recompute_tiers"]
        # recompute_tiers returns per-tier rowcounts; an error dict would mean
        # the phase blew up.
        assert isinstance(tiers, dict) and "error" not in tiers
        assert set(tiers).issubset({"core", "working", "peripheral"})
