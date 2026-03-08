"""Tests for compact_summaries job — LLM-powered summarization of archived claims."""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from memorymaster.jobs.compact_summaries import (
    CompactSummaryResult,
    _build_claim_text_block,
    _cluster_by_subject,
    _get_unsummarized_archived_claims,
    run,
)
from memorymaster.models import CitationInput, ClaimLink
from memorymaster.storage import SQLiteStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test.db"
    s = SQLiteStore(db_path)
    s.init_db()
    return s


def _create_archived_claim(store, text, subject="test-subject", predicate="has_value", obj="val"):
    """Helper: create a claim and transition it to archived."""
    claim = store.create_claim(
        text=text,
        citations=[CitationInput(source="test")],
        subject=subject,
        predicate=predicate,
        object_value=obj,
    )
    # Transition to confirmed first, then archived
    from memorymaster.lifecycle import transition_claim
    transition_claim(store, claim.id, to_status="confirmed", reason="test", event_type="transition")
    transition_claim(store, claim.id, to_status="stale", reason="test", event_type="decay")
    transition_claim(store, claim.id, to_status="archived", reason="test", event_type="compactor")
    return store.get_claim(claim.id)


class TestClusterBySubject:
    def test_groups_by_subject(self, store):
        claims = [
            _create_archived_claim(store, f"fact {i}", subject="dns-config")
            for i in range(4)
        ]
        claims += [
            _create_archived_claim(store, f"other {i}", subject="ssh-access")
            for i in range(3)
        ]
        clusters = _cluster_by_subject(claims)
        assert "dns-config" in clusters
        assert "ssh-access" in clusters
        assert len(clusters["dns-config"]) == 4
        assert len(clusters["ssh-access"]) == 3

    def test_none_subject_grouped_as_unknown(self, store):
        claim = _create_archived_claim(store, "no subject", subject=None)
        clusters = _cluster_by_subject([claim])
        assert "unknown" in clusters


class TestBuildClaimTextBlock:
    def test_formats_claims(self, store):
        claims = [_create_archived_claim(store, "test fact", subject="topic", predicate="is", obj="good")]
        block = _build_claim_text_block(claims)
        assert "[1]" in block
        assert "Subject: topic" in block
        assert "Predicate: is" in block
        assert "Value: good" in block


class TestGetUnsummarizedArchivedClaims:
    def test_returns_archived_without_derived_links(self, store):
        claims = [
            _create_archived_claim(store, f"fact {i}", subject="topic")
            for i in range(3)
        ]
        unsummarized = _get_unsummarized_archived_claims(store)
        assert len(unsummarized) == 3

    def test_excludes_already_summarized(self, store):
        claims = [
            _create_archived_claim(store, f"fact {i}", subject="topic", predicate=f"pred_{i}")
            for i in range(3)
        ]
        # Create a summary claim and link it to the first archived claim
        summary = store.create_claim(
            text="summary",
            citations=[CitationInput(source="compact-summaries")],
            subject="topic",
            predicate="summary_of",
        )
        store.add_claim_link(
            source_id=summary.id,
            target_id=claims[0].id,
            link_type="derived_from",
        )
        unsummarized = _get_unsummarized_archived_claims(store)
        unsummarized_ids = {c.id for c in unsummarized}
        assert claims[0].id not in unsummarized_ids
        assert claims[1].id in unsummarized_ids
        assert claims[2].id in unsummarized_ids


class TestRunDryRun:
    def test_dry_run_no_db_changes(self, store):
        for i in range(5):
            _create_archived_claim(store, f"dns fact {i}", subject="dns-config", predicate=f"fact_{i}")

        result = run(
            store,
            min_cluster=3,
            dry_run=True,
        )
        assert result.dry_run is True
        assert result.clusters_found >= 1
        assert result.summaries_created >= 1
        assert result.source_claims_summarized >= 3
        # No summary claims should have been created
        confirmed = store.list_claims(status="confirmed")
        assert len(confirmed) == 0

    def test_below_min_cluster_skipped(self, store):
        # Only 2 claims - below min_cluster=3
        for i in range(2):
            _create_archived_claim(store, f"rare fact {i}", subject="rare-topic", predicate=f"fact_{i}")

        result = run(store, min_cluster=3, dry_run=True)
        assert result.clusters_found == 0
        assert result.summaries_created == 0


class TestRunWithMockedLLM:
    @patch("memorymaster.jobs.compact_summaries._call_llm")
    def test_creates_summary_claim(self, mock_llm, store):
        mock_llm.return_value = json.dumps({
            "summary_text": "DNS config uses Cloudflare with specific records for failover.",
            "subject": "dns-config",
            "predicate": "summary_of",
            "object_value": "Cloudflare DNS with failover records",
            "confidence": 0.9,
        })

        for i in range(4):
            _create_archived_claim(
                store,
                f"DNS record {i} points to cloudflare",
                subject="dns-config",
                predicate=f"record_{i}",
                obj=f"value_{i}",
            )

        result = run(
            store,
            provider="gemini",
            api_key="fake-key",
            min_cluster=3,
            dry_run=False,
        )

        assert result.summaries_created == 1
        assert result.source_claims_summarized == 4
        assert result.errors == 0

        # Verify summary claim was created
        all_candidates = store.list_claims(status="candidate")
        all_confirmed = store.list_claims(status="confirmed")
        # The summary claim should be confirmed
        summary_claims = [c for c in all_confirmed if c.claim_type == "summary"]
        assert len(summary_claims) == 1
        assert "Cloudflare" in summary_claims[0].text

        # Verify derived_from links were created
        links = store.get_claim_links(summary_claims[0].id)
        derived_links = [l for l in links if l.link_type == "derived_from"]
        assert len(derived_links) == 4

    @patch("memorymaster.jobs.compact_summaries._call_llm")
    def test_handles_llm_error(self, mock_llm, store):
        mock_llm.side_effect = Exception("API timeout")

        for i in range(3):
            _create_archived_claim(store, f"fact {i}", subject="topic", predicate=f"pred_{i}")

        result = run(
            store,
            provider="gemini",
            api_key="fake-key",
            min_cluster=3,
        )

        assert result.errors == 1
        assert result.summaries_created == 0

    @patch("memorymaster.jobs.compact_summaries._call_llm")
    def test_handles_empty_llm_response(self, mock_llm, store):
        mock_llm.return_value = ""

        for i in range(3):
            _create_archived_claim(store, f"fact {i}", subject="topic", predicate=f"pred_{i}")

        result = run(
            store,
            provider="gemini",
            api_key="fake-key",
            min_cluster=3,
        )

        assert result.errors == 1
        assert result.summaries_created == 0

    @patch("memorymaster.jobs.compact_summaries._call_llm")
    def test_multiple_clusters(self, mock_llm, store):
        call_count = 0

        def mock_response(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return json.dumps({
                "summary_text": f"Summary {call_count} of related claims.",
                "subject": f"topic-{call_count}",
                "predicate": "summary_of",
                "object_value": f"conclusion {call_count}",
                "confidence": 0.85,
            })

        mock_llm.side_effect = mock_response

        # Create two distinct clusters
        for i in range(4):
            _create_archived_claim(store, f"alpha fact {i}", subject="alpha", predicate=f"alpha_pred_{i}")
        for i in range(3):
            _create_archived_claim(store, f"beta fact {i}", subject="beta", predicate=f"beta_pred_{i}")

        result = run(
            store,
            provider="gemini",
            api_key="fake-key",
            min_cluster=3,
        )

        assert result.clusters_found == 2
        assert result.summaries_created == 2
        assert result.source_claims_summarized == 7


class TestRunNoArchivedClaims:
    def test_empty_result(self, store):
        result = run(store, min_cluster=3, dry_run=True)
        assert result.clusters_found == 0
        assert result.summaries_created == 0
        assert result.source_claims_summarized == 0


class TestMaxClusterSplitting:
    @patch("memorymaster.jobs.compact_summaries._call_llm")
    def test_splits_large_clusters(self, mock_llm, store):
        mock_llm.return_value = json.dumps({
            "summary_text": "Summary of large cluster.",
            "subject": "big-topic",
            "predicate": "summary_of",
            "object_value": "combined findings",
            "confidence": 0.8,
        })

        # Create 8 claims with same subject, max_cluster=4
        for i in range(8):
            _create_archived_claim(store, f"fact {i}", subject="big-topic", predicate=f"pred_{i}")

        result = run(
            store,
            provider="gemini",
            api_key="fake-key",
            min_cluster=3,
            max_cluster=4,
        )

        # Should create 2 summaries (8 / 4 = 2 sub-clusters)
        assert result.summaries_created == 2
        assert result.source_claims_summarized == 8
