from __future__ import annotations

import pytest

from memorymaster.surfaces.cli import build_parser
from memorymaster.recall.context_optimizer import ContextResult, PROVIDERS, pack_context
from memorymaster.models import Claim
from memorymaster.service import MemoryService


def _make_claim(
    claim_id: int,
    text: str,
    *,
    claim_type: str | None = "fact",
    scope: str = "project",
    volatility: str = "medium",
    status: str = "confirmed",
    confidence: float = 0.8,
    pinned: bool = False,
) -> Claim:
    return Claim(
        id=claim_id,
        text=text,
        idempotency_key=None,
        normalized_text=None,
        claim_type=claim_type,
        subject=None,
        predicate=None,
        object_value=None,
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


def _row(claim: Claim, score: float) -> dict:
    return {"claim": claim, "score": score}


def test_provider_choices_include_auto_and_named_providers():
    assert PROVIDERS == ("claude_cli", "google", "openai", "anthropic", "ollama", "auto")


def test_default_provider_none_preserves_ranked_order():
    rows = [
        _row(_make_claim(1, "high score volatile", volatility="high"), 0.99),
        _row(_make_claim(2, "lower score stable", volatility="low"), 0.50),
    ]

    output = pack_context(rows, token_budget=1000, output_format="text").output

    assert output.index("high score volatile") < output.index("lower score stable")


def test_claude_cli_prefers_stable_large_chunk_ordering():
    rows = [
        _row(_make_claim(1, "high score volatile", volatility="high"), 0.99),
        _row(_make_claim(2, "lower score stable", volatility="low"), 0.50),
    ]

    output = pack_context(
        rows,
        token_budget=1000,
        output_format="text",
        provider="claude_cli",
    ).output

    assert output.index("lower score stable") < output.index("high score volatile")


def test_google_keeps_score_order_for_many_small_chunks():
    rows = [
        _row(_make_claim(1, "high score volatile", volatility="high"), 0.99),
        _row(_make_claim(2, "lower score stable", volatility="low"), 0.50),
    ]

    output = pack_context(
        rows,
        token_budget=1000,
        output_format="text",
        provider="google",
    ).output

    assert output.index("high score volatile") < output.index("lower score stable")


def test_auto_small_budget_uses_dense_ollama_ordering():
    rows = [
        _row(_make_claim(1, "high score " + ("long text " * 120)), 0.95),
        _row(_make_claim(2, "short dense fact"), 0.70),
    ]

    output = pack_context(
        rows,
        token_budget=800,
        output_format="text",
        provider="auto",
    ).output

    assert output.index("short dense fact") < output.index("high score")


def test_invalid_provider_raises():
    with pytest.raises(ValueError, match="Unknown provider"):
        pack_context([], provider="not-a-provider")


def test_cli_context_accepts_provider_flag():
    args = build_parser().parse_args(["context", "repo facts", "--provider", "openai"])

    assert args.provider == "openai"


def test_service_query_for_context_threads_provider(monkeypatch):
    service = MemoryService.__new__(MemoryService)
    service.query_rows = lambda **_: []
    called = {}

    def fake_pack_context(rows, **kwargs):
        called["rows"] = rows
        called.update(kwargs)
        return ContextResult(
            output="",
            claims_considered=0,
            claims_included=0,
            tokens_used=0,
            token_budget=kwargs["token_budget"],
            format=kwargs["output_format"],
        )

    monkeypatch.setattr("memorymaster.service.pack_context", fake_pack_context)

    service.query_for_context("repo facts", provider="anthropic")

    assert called["provider"] == "anthropic"
