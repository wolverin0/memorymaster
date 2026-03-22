"""Tests for context_hook — automatic memory extraction and injection.

Tests cover:
  - classify_observation: pattern matching for observation types
  - recall: mocked query against memory database
  - observe: mocked ingestion of observations
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from memorymaster.context_hook import classify_observation, observe, recall


class TestClassifyObservation:
    """Test observation pattern classification."""

    def test_classify_preference(self) -> None:
        """Preference pattern should match 'don't' and 'prefer'."""
        assert classify_observation("I prefer to use Python") == "preference"
        assert classify_observation("Don't use that library") == "preference"
        assert classify_observation("Please always test before shipping") == "preference"

    def test_classify_decision(self) -> None:
        """Decision pattern should match 'decided', 'will', etc."""
        assert classify_observation("We decided to use React") == "decision"
        assert classify_observation("Let's use PostgreSQL") == "decision"
        assert classify_observation("Going to migrate to TypeScript") == "decision"

    def test_classify_constraint(self) -> None:
        """Constraint pattern should match 'must', 'require', etc."""
        assert classify_observation("Must support Python 3.8+") == "constraint"
        assert classify_observation("Mandatory HTTPS for all endpoints") == "constraint"
        assert classify_observation("This is a critical requirement") == "constraint"

    def test_classify_fact(self) -> None:
        """Fact pattern should match architecture/tech choices."""
        assert classify_observation("Using FastAPI for this service") == "fact"
        assert classify_observation("Deployed to AWS") == "fact"
        assert classify_observation("Installed PostgreSQL 13") == "fact"

    def test_classify_event(self) -> None:
        """Event pattern should match bugs and crashes."""
        assert classify_observation("Fixed the auth bug") == "event"
        assert classify_observation("There is a bug in the code") == "event"
        assert classify_observation("ERROR in the system") == "event"

    def test_classify_commitment(self) -> None:
        """Commitment pattern should match todos and next steps."""
        assert classify_observation("TODO: refactor the auth module") == "commitment"
        assert classify_observation("Will do this next") == "commitment"
        assert classify_observation("I should refactor this later") == "commitment"

    def test_classify_none_for_irrelevant(self) -> None:
        """Irrelevant text should return None."""
        assert classify_observation("Hello world") is None
        assert classify_observation("This is just a greeting") is None
        assert classify_observation("123456") is None

    def test_classify_case_insensitive(self) -> None:
        """Pattern matching should be case-insensitive."""
        assert classify_observation("DECIDED to use TypeScript") == "decision"
        assert classify_observation("MuSt validate input") == "constraint"


class TestRecall:
    """Test memory recall function (mocked)."""

    @patch("memorymaster.service.MemoryService")
    def test_recall_with_results(self, mock_service_class: MagicMock) -> None:
        """Recall should return formatted context when claims are found."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        # Mock the result
        mock_result = MagicMock()
        mock_result.claims_included = 3
        mock_result.output = "claim 1\nclaim 2\nclaim 3"
        mock_service.query_for_context.return_value = mock_result

        result = recall("what am I working on?", db_path=":memory:")

        assert result == "claim 1\nclaim 2\nclaim 3"
        mock_service.query_for_context.assert_called_once()
        call_kwargs = mock_service.query_for_context.call_args.kwargs
        assert call_kwargs["query"] == "what am I working on?"
        assert call_kwargs["retrieval_mode"] == "legacy"

    @patch("memorymaster.service.MemoryService")
    def test_recall_no_results(self, mock_service_class: MagicMock) -> None:
        """Recall should return empty string when no claims found."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_result = MagicMock()
        mock_result.claims_included = 0
        mock_service.query_for_context.return_value = mock_result

        result = recall("what am I working on?", db_path=":memory:")

        assert result == ""

    @patch("memorymaster.service.MemoryService")
    def test_recall_with_custom_budget(self, mock_service_class: MagicMock) -> None:
        """Recall should respect custom token budget."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_result = MagicMock()
        mock_result.claims_included = 1
        mock_result.output = "result"
        mock_service.query_for_context.return_value = mock_result

        recall("test", db_path=":memory:", budget=5000)

        call_kwargs = mock_service.query_for_context.call_args.kwargs
        assert call_kwargs["token_budget"] == 5000

    @patch("memorymaster.service.MemoryService")
    def test_recall_sanitizes_non_ascii(self, mock_service_class: MagicMock) -> None:
        """Recall should sanitize non-ASCII characters for Windows console."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_result = MagicMock()
        mock_result.claims_included = 1
        mock_result.output = "claim with ñ and é characters"
        mock_service.query_for_context.return_value = mock_result

        result = recall("test", db_path=":memory:")

        # Non-ASCII chars should be replaced with ?
        assert "claim" in result


class TestObserve:
    """Test observation ingestion (mocked)."""

    @patch("memorymaster.service.MemoryService")
    def test_observe_with_pattern_match(self, mock_service_class: MagicMock) -> None:
        """Observe should ingest text that matches a pattern."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 42
        mock_service.ingest.return_value = mock_claim

        result = observe("We decided to use React", source="session")

        assert result["ingested"] is True
        assert result["claim_type"] == "decision"
        assert result["claim_id"] == 42

        mock_service.ingest.assert_called_once()
        call_kwargs = mock_service.ingest.call_args.kwargs
        assert call_kwargs["text"] == "We decided to use React"
        assert call_kwargs["claim_type"] == "decision"
        assert call_kwargs["source_agent"] == "context-hook"

    @patch("memorymaster.service.MemoryService")
    def test_observe_no_pattern_match_with_auto_classify(self, mock_service_class: MagicMock) -> None:
        """Observe should skip ingestion if text doesn't match pattern and auto_classify=True."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        result = observe("Just a random sentence", source="session", auto_classify=True)

        assert result["ingested"] is False
        assert result["claim_type"] is None
        assert result["reason"] == "no_pattern_match"
        mock_service.ingest.assert_not_called()

    @patch("memorymaster.service.MemoryService")
    def test_observe_force_ingest_without_pattern(self, mock_service_class: MagicMock) -> None:
        """Observe should ingest even without pattern match if force=True."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 99
        mock_service.ingest.return_value = mock_claim

        result = observe("Just a random sentence", source="session", force=True)

        assert result["ingested"] is True
        assert result["claim_type"] == "fact"
        assert result["claim_id"] == 99

    @patch("memorymaster.service.MemoryService")
    def test_observe_auto_classify_false(self, mock_service_class: MagicMock) -> None:
        """Observe should ingest regardless of pattern if auto_classify=False."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 77
        mock_service.ingest.return_value = mock_claim

        result = observe("Just a random sentence", source="session", auto_classify=False)

        assert result["ingested"] is True
        assert result["claim_type"] == "fact"
        assert result["claim_id"] == 77

    @patch("memorymaster.service.MemoryService")
    def test_observe_truncates_long_text(self, mock_service_class: MagicMock) -> None:
        """Observe should truncate text to 2000 chars."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 1
        mock_service.ingest.return_value = mock_claim

        long_text = "We decided to do X. " * 200  # ~3000 chars
        observe(long_text, source="session")

        call_kwargs = mock_service.ingest.call_args.kwargs
        assert len(call_kwargs["text"]) <= 2000

    @patch("memorymaster.service.MemoryService")
    def test_observe_handles_ingest_failure(self, mock_service_class: MagicMock) -> None:
        """Observe should handle exceptions during ingestion gracefully."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.ingest.side_effect = ValueError("DB error")

        result = observe("We decided to use React", source="session")

        assert result["ingested"] is False
        assert "DB error" in result.get("reason", "")

    @patch("memorymaster.service.MemoryService")
    def test_observe_with_custom_scope(self, mock_service_class: MagicMock) -> None:
        """Observe should respect custom scope parameter."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 1
        mock_service.ingest.return_value = mock_claim

        observe("We decided to use React", source="session", scope="personal")

        call_kwargs = mock_service.ingest.call_args.kwargs
        assert call_kwargs["scope"] == "personal"

    @patch("memorymaster.service.MemoryService")
    def test_observe_sets_confidence(self, mock_service_class: MagicMock) -> None:
        """Observe should set confidence to 0.6."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 1
        mock_service.ingest.return_value = mock_claim

        observe("We decided to use React", source="session")

        call_kwargs = mock_service.ingest.call_args.kwargs
        assert call_kwargs["confidence"] == 0.6
