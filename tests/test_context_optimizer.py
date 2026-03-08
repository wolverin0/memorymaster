"""Tests for the context window optimizer (P1 feature #9)."""

from __future__ import annotations

import json

import pytest

from memorymaster.context_optimizer import (
    ContextResult,
    OUTPUT_FORMATS,
    estimate_tokens,
    pack_context,
)
from memorymaster.models import Claim


def _make_claim(
    id: int = 1,
    text: str = "Test claim",
    status: str = "confirmed",
    confidence: float = 0.8,
    pinned: bool = False,
    subject: str | None = None,
    predicate: str | None = None,
    object_value: str | None = None,
    scope: str = "project",
    volatility: str = "medium",
) -> Claim:
    return Claim(
        id=id,
        text=text,
        idempotency_key=None,
        normalized_text=None,
        claim_type=None,
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        scope=scope,
        volatility=volatility,
        status=status,
        confidence=confidence,
        pinned=pinned,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        last_validated_at=None,
        archived_at=None,
        citations=[],
    )


def _make_row(claim: Claim, score: float = 0.9) -> dict:
    return {
        "claim": claim,
        "score": score,
        "lexical_score": 0.5,
        "freshness_score": 0.8,
        "confidence_score": claim.confidence,
        "vector_score": 0.0,
        "annotation": {"status": claim.status, "active": True, "stale": False, "conflicted": False, "pinned": False},
    }


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 1  # minimum 1

    def test_short_string(self):
        assert estimate_tokens("hello") == 1

    def test_longer_string(self):
        text = "a" * 400
        assert estimate_tokens(text) == 100


class TestPackContextText:
    def test_empty_rows(self):
        result = pack_context([], token_budget=4000, output_format="text")
        assert isinstance(result, ContextResult)
        assert result.claims_included == 0
        assert result.claims_considered == 0
        assert "no claims" in result.output.lower()

    def test_single_claim(self):
        rows = [_make_row(_make_claim(text="Python uses indentation"))]
        result = pack_context(rows, token_budget=4000, output_format="text")
        assert result.claims_included == 1
        assert result.claims_considered == 1
        assert "Python uses indentation" in result.output
        assert "1/1 claims" in result.output

    def test_budget_respected(self):
        rows = [
            _make_row(_make_claim(id=i, text=f"Claim number {i} with some extra text to use tokens"), score=1.0 - i * 0.01)
            for i in range(50)
        ]
        result = pack_context(rows, token_budget=200, output_format="text")
        assert result.tokens_used <= result.token_budget
        assert result.claims_included < 50

    def test_with_triple(self):
        claim = _make_claim(subject="Python", predicate="uses", object_value="indentation")
        rows = [_make_row(claim)]
        result = pack_context(rows, token_budget=4000, output_format="text")
        assert "Python | uses | indentation" in result.output

    def test_stale_status_shown(self):
        claim = _make_claim(status="stale")
        rows = [_make_row(claim)]
        result = pack_context(rows, token_budget=4000, output_format="text")
        assert "[stale]" in result.output

    def test_pinned_shown(self):
        claim = _make_claim(pinned=True)
        rows = [_make_row(claim)]
        result = pack_context(rows, token_budget=4000, output_format="text")
        assert "[pinned]" in result.output


class TestPackContextXml:
    def test_empty(self):
        result = pack_context([], token_budget=4000, output_format="xml")
        assert "<memory-context>" in result.output
        assert "</memory-context>" in result.output
        assert result.claims_included == 0

    def test_single_claim(self):
        rows = [_make_row(_make_claim(text="Test fact"))]
        result = pack_context(rows, token_budget=4000, output_format="xml")
        assert "<claim " in result.output
        assert "<text>Test fact</text>" in result.output
        assert "</claim>" in result.output
        assert '<meta claims_included="1"' in result.output

    def test_xml_escaping(self):
        claim = _make_claim(text='Value is <100 & "special"')
        rows = [_make_row(claim)]
        result = pack_context(rows, token_budget=4000, output_format="xml")
        assert "&lt;100" in result.output
        assert "&amp;" in result.output
        assert "&quot;" in result.output

    def test_with_triple(self):
        claim = _make_claim(subject="DB", predicate="type", object_value="PostgreSQL")
        rows = [_make_row(claim)]
        result = pack_context(rows, token_budget=4000, output_format="xml")
        assert '<triple subject="DB"' in result.output


class TestPackContextJson:
    def test_empty(self):
        result = pack_context([], token_budget=4000, output_format="json")
        data = json.loads(result.output)
        assert data["claims"] == []
        assert data["meta"]["claims_included"] == 0

    def test_single_claim(self):
        rows = [_make_row(_make_claim(id=42, text="The answer"))]
        result = pack_context(rows, token_budget=4000, output_format="json")
        data = json.loads(result.output)
        assert len(data["claims"]) == 1
        assert data["claims"][0]["id"] == 42
        assert data["claims"][0]["text"] == "The answer"
        assert data["meta"]["claims_included"] == 1
        assert data["meta"]["token_budget"] == 4000

    def test_budget_in_json_meta(self):
        rows = [_make_row(_make_claim(text="x" * 100))]
        result = pack_context(rows, token_budget=4000, output_format="json")
        data = json.loads(result.output)
        assert data["meta"]["tokens_used"] <= 4000


class TestPackContextEdgeCases:
    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Unknown format"):
            pack_context([], output_format="yaml")

    def test_zero_budget(self):
        with pytest.raises(ValueError, match="positive"):
            pack_context([], token_budget=0)

    def test_greedy_knapsack_skips_large_keeps_small(self):
        """Large claim skipped, smaller claim after it still included."""
        large = _make_claim(id=1, text="x" * 2000)
        small = _make_claim(id=2, text="small fact")
        rows = [_make_row(large, score=0.9), _make_row(small, score=0.8)]
        result = pack_context(rows, token_budget=200, output_format="text")
        assert result.claims_included >= 1
        assert "small fact" in result.output

    def test_all_formats_produce_output(self):
        rows = [_make_row(_make_claim(text="Test"))]
        for fmt in OUTPUT_FORMATS:
            result = pack_context(rows, token_budget=4000, output_format=fmt)
            assert result.output
            assert result.format == fmt
            assert result.claims_included == 1
