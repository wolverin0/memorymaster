"""Tests for automatic conflict resolution."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


from memorymaster.govern.auto_resolver import _cite_summary, _llm_evaluate, resolve_conflict_pair


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
    """Test LLM evaluation via the centralized llm_provider.call_llm."""

    @patch("memorymaster.govern.auto_resolver.call_llm")
    def test_llm_evaluate_success(self, mock_call_llm):
        """Successful LLM evaluation returns parsed JSON."""
        mock_call_llm.return_value = '{"winner": "A", "reason": "more recent"}'

        result = _llm_evaluate("test prompt")
        assert result["winner"] == "A"
        assert "more recent" in result["reason"]

    @patch("memorymaster.govern.auto_resolver.call_llm")
    def test_llm_evaluate_with_markdown_fence(self, mock_call_llm):
        """LLM response with markdown fence is parsed."""
        mock_call_llm.return_value = '```json\n{"winner": "B", "reason": "specific"}\n```'

        result = _llm_evaluate("test prompt")
        assert result["winner"] == "B"

    @patch("memorymaster.govern.auto_resolver.call_llm")
    def test_llm_evaluate_failure_returns_empty(self, mock_call_llm):
        """LLM failure returns empty dict."""
        mock_call_llm.side_effect = Exception("Provider error")
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

    @patch("memorymaster.govern.auto_resolver._llm_evaluate")
    def test_resolve_conflict_pair_lllm_called(self, mock_llm):
        """resolve_conflict_pair calls LLM with formatted prompt."""
        mock_llm.return_value = {"winner": "A", "reason": "test"}
        mock_store = MagicMock()
        mock_store.get_claim.return_value = MagicMock(replaced_by_claim_id=1)

        claim_a = self.make_claim(1, "Claim A", 0.8, "2024-01-01")
        claim_b = self.make_claim(2, "Claim B", 0.6, "2024-01-02")

        result = resolve_conflict_pair(mock_store, claim_a, claim_b)

        mock_llm.assert_called_once()
        prompt_arg = mock_llm.call_args[0][0]
        assert "Claim A" in prompt_arg
        assert "Claim B" in prompt_arg
        mock_store.mark_superseded.assert_called_once_with(
            2,
            1,
            "llm_conflict_resolution: test",
        )

    @patch("memorymaster.govern.auto_resolver._llm_evaluate")
    def test_resolve_conflict_pair_winner_a(self, mock_llm):
        """Resolves with winner A."""
        mock_llm.return_value = {"winner": "A", "reason": "better evidence"}
        mock_store = MagicMock()
        mock_store.get_claim.return_value = MagicMock(replaced_by_claim_id=1)

        claim_a = self.make_claim(1, "A", 0.8, "2024-01-01")
        claim_b = self.make_claim(2, "B", 0.6, "2024-01-02")

        result = resolve_conflict_pair(mock_store, claim_a, claim_b)
        assert result["resolved"] is True
        mock_store.mark_superseded.assert_called_once()

    @patch("memorymaster.govern.auto_resolver._llm_evaluate")
    def test_resolve_conflict_pair_no_result(self, mock_llm):
        """No result from LLM returns error."""
        mock_llm.return_value = {}
        mock_store = MagicMock()

        claim_a = self.make_claim(1, "A", 0.8, "2024-01-01")
        claim_b = self.make_claim(2, "B", 0.6, "2024-01-02")

        result = resolve_conflict_pair(mock_store, claim_a, claim_b)
        assert "error" in result or "winner" not in result


def test_resolution_prompt_never_carries_a_private_address_and_a_blocked_claim_makes_no_call():
    """S-6: the claims and citations pass the egress gate before the judge prompt is built."""
    from types import SimpleNamespace

    cite = SimpleNamespace(source="notes", locator="/home/" + "jdoe" + "/a.md")

    def claim(i, text):
        return SimpleNamespace(id=i, text=text, confidence=0.5, updated_at="2026-01-01", citations=[cite])

    prompts: list[str] = []
    with patch("memorymaster.govern.auto_resolver.call_llm", side_effect=lambda p, t="": prompts.append(p) or "{}"):
        resolve_conflict_pair(MagicMock(), claim(1, "db at 10.1.2.3 now"), claim(2, "db at 10.1.2.3 later"))
        blocked = resolve_conflict_pair(MagicMock(), claim(3, "DB_PASS=" + "hunter2hunter2"), claim(4, "ok"))
    assert len(prompts) == 1 and "10.1.2.3" not in prompts[0] and "jdoe" not in prompts[0]
    assert blocked["reason"] == "egress_withheld"
