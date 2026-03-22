"""Tests for automatic conflict resolution."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from memorymaster.auto_resolver import _cite_summary, _llm_evaluate, resolve_conflict_pair


class TestCiteSummary:
    """Test citation summarization."""

    def test_cite_summary_no_citations(self):
        """No citations returns (none)."""
        claim = MagicMock(citations=None)
        assert _cite_summary(claim) == "(none)"

    def test_cite_summary_empty_citations(self):
        """Empty citations returns (none)."""
        claim = MagicMock(citations=[])
        assert _cite_summary(claim) == "(none)"

    def test_cite_summary_single_citation(self):
        """Single citation is formatted."""
        cite = MagicMock(source="file.py", locator="line 42")
        claim = MagicMock(citations=[cite])
        result = _cite_summary(claim)
        assert "file.py" in result
        assert "line 42" in result

    def test_cite_summary_multiple_citations(self):
        """Multiple citations are limited to 3."""
        cites = [
            MagicMock(source=f"file{i}.py", locator=f"line {i}")
            for i in range(5)
        ]
        claim = MagicMock(citations=cites)
        result = _cite_summary(claim)
        assert "file0.py" in result
        assert "file4.py" not in result  # Only first 3

    def test_cite_summary_no_locator(self):
        """Citation without locator omits locator."""
        cite = MagicMock(source="doc.md", locator=None)
        claim = MagicMock(citations=[cite])
        result = _cite_summary(claim)
        assert "doc.md" in result


class TestLlmEvaluate:
    """Test LLM evaluation."""

    @patch("memorymaster.auto_resolver.urllib.request.urlopen")
    def test_llm_evaluate_success(self, mock_urlopen):
        """Successful LLM evaluation returns parsed JSON."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "message": {"content": '{"winner": "A", "reason": "more recent"}'}
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = _llm_evaluate("test prompt")
        assert result["winner"] == "A"
        assert "more recent" in result["reason"]

    @patch("memorymaster.auto_resolver.urllib.request.urlopen")
    def test_llm_evaluate_with_markdown_fence(self, mock_urlopen):
        """LLM response with markdown fence is parsed."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "message": {"content": '```json\n{"winner": "B", "reason": "specific"}\n```'}
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = _llm_evaluate("test prompt")
        assert result["winner"] == "B"

    @patch("memorymaster.auto_resolver.urllib.request.urlopen")
    def test_llm_evaluate_failure_returns_empty(self, mock_urlopen):
        """LLM failure returns empty dict."""
        mock_urlopen.side_effect = Exception("Network error")
        result = _llm_evaluate("test prompt")
        assert result == {}


class TestResolveConflictPair:
    """Test conflict pair resolution."""

    def make_claim(self, id_val, text, confidence, updated):
        """Helper to create mock claim."""
        return MagicMock(
            id=id_val,
            text=text,
            confidence=confidence,
            updated_at=updated,
            citations=[],
        )

    @patch("memorymaster.auto_resolver._llm_evaluate")
    @patch("memorymaster.auto_resolver.transition_claim")
    def test_resolve_conflict_pair_lllm_called(self, mock_transition, mock_llm):
        """resolve_conflict_pair calls LLM with formatted prompt."""
        mock_llm.return_value = {"winner": "A", "reason": "test"}
        mock_store = MagicMock()

        claim_a = self.make_claim(1, "Claim A", 0.8, "2024-01-01")
        claim_b = self.make_claim(2, "Claim B", 0.6, "2024-01-02")

        result = resolve_conflict_pair(mock_store, claim_a, claim_b)

        mock_llm.assert_called_once()
        prompt_arg = mock_llm.call_args[0][0]
        assert "Claim A" in prompt_arg
        assert "Claim B" in prompt_arg

    @patch("memorymaster.auto_resolver._llm_evaluate")
    @patch("memorymaster.auto_resolver.transition_claim")
    def test_resolve_conflict_pair_winner_a(self, mock_transition, mock_llm):
        """Resolves with winner A."""
        mock_llm.return_value = {"winner": "A", "reason": "better evidence"}
        mock_store = MagicMock()

        claim_a = self.make_claim(1, "A", 0.8, "2024-01-01")
        claim_b = self.make_claim(2, "B", 0.6, "2024-01-02")

        result = resolve_conflict_pair(mock_store, claim_a, claim_b)
        assert mock_transition.called

    @patch("memorymaster.auto_resolver._llm_evaluate")
    @patch("memorymaster.auto_resolver.transition_claim")
    def test_resolve_conflict_pair_no_result(self, mock_transition, mock_llm):
        """No result from LLM returns error."""
        mock_llm.return_value = {}
        mock_store = MagicMock()

        claim_a = self.make_claim(1, "A", 0.8, "2024-01-01")
        claim_b = self.make_claim(2, "B", 0.6, "2024-01-02")

        result = resolve_conflict_pair(mock_store, claim_a, claim_b)
        assert "error" in result or "winner" not in result
