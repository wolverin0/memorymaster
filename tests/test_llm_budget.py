"""Tests for the per-cycle LLM budget caps (v3.19.0-H1).

Covers:
- No-caps default = unchanged behaviour (back-compat)
- Per-cycle call-count cap raises LLMBudgetExceeded with reason=calls_exhausted
- Per-cycle token cap raises with reason=tokens_exhausted
- Per-cycle per-provider failure cap raises with reason=provider_failures_exhausted
  + acts as circuit breaker on subsequent calls to the same provider
- service.run_cycle surfaces aborted/reason/provider in result['budget']
  when a cap is hit mid-cycle
- Abort-reason logging fires when run_cycle is aborted
"""
from __future__ import annotations

import logging
from typing import Iterator

import pytest

from memorymaster import llm_provider
from memorymaster.govern import llm_budget


@pytest.fixture(autouse=True)
def _clear_budget_env(monkeypatch) -> Iterator[None]:
    """Ensure each test starts with all budget env vars unset and no active scope."""
    for var in (
        "MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE",
        "MEMORYMASTER_MAX_TOKENS_PER_CYCLE",
        "MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE",
        "MEMORYMASTER_LLM_FALLBACK_PROVIDER",
        "MEMORYMASTER_LLM_FALLBACK_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    # Force a known primary provider with a stub function so call_llm
    # doesn't touch the network.
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
    yield


def _stub_provider(monkeypatch, response: str | list[str]) -> list[int]:
    """Replace the 'google' provider with a function returning controlled values.

    Returns a counter list so callers can assert how many times the stub fired.
    Pass a string for a constant response, or a list of strings to cycle through.
    """
    calls = [0]
    if isinstance(response, str):
        sequence = [response]
    else:
        sequence = list(response)

    def stub(prompt: str, text: str) -> str:
        calls[0] += 1
        return sequence[min(calls[0] - 1, len(sequence) - 1)]

    monkeypatch.setitem(llm_provider._PROVIDERS, "google", stub)
    return calls


# ---------------------------------------------------------------------------
# Back-compat: caps unset = no enforcement
# ---------------------------------------------------------------------------


def test_no_caps_no_enforcement_preserves_behaviour(monkeypatch):
    calls = _stub_provider(monkeypatch, "ok")
    # Even inside a scope, with no caps, many calls succeed.
    with llm_budget.cycle_scope() as budget:
        for _ in range(5):
            assert llm_provider.call_llm("p", "t") == "ok"
    assert calls[0] == 5
    snap = budget.snapshot()
    assert snap["calls"] == 5
    assert snap["aborted_reason"] is None


def test_call_outside_scope_is_unaffected(monkeypatch):
    """Calls made without an active cycle_scope must not raise or count."""
    calls = _stub_provider(monkeypatch, "ok")
    # No scope opened. Even if env caps were set, they shouldn't fire.
    monkeypatch.setenv("MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE", "1")
    assert llm_provider.call_llm("p", "t") == "ok"
    assert llm_provider.call_llm("p", "t") == "ok"
    assert calls[0] == 2
    assert llm_budget.get_current() is None


# ---------------------------------------------------------------------------
# Per-cycle call-count cap
# ---------------------------------------------------------------------------


def test_calls_cap_raises_with_reason(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE", "2")
    calls = _stub_provider(monkeypatch, "ok")

    with llm_budget.cycle_scope() as budget:
        llm_provider.call_llm("a", "b")  # 1
        llm_provider.call_llm("a", "b")  # 2 — at cap
        with pytest.raises(llm_budget.LLMBudgetExceeded) as exc:
            llm_provider.call_llm("a", "b")  # 3 — should be blocked before provider call
        assert exc.value.reason == "calls_exhausted"
        assert exc.value.provider is None

    # Provider was called only twice — the third was blocked before contacting it.
    assert calls[0] == 2
    assert budget.aborted_reason == "calls_exhausted"


# ---------------------------------------------------------------------------
# Per-cycle token cap
# ---------------------------------------------------------------------------


def test_tokens_cap_raises_with_reason(monkeypatch):
    # estimate_tokens is roughly (sum_chars + 3) // 4. One call with prompt
    # of 200 chars + response of 200 chars ≈ 100 tokens. Cap at 50 → first
    # call records calls=1, tokens≈100 ≥ 50, raises tokens_exhausted.
    monkeypatch.setenv("MEMORYMASTER_MAX_TOKENS_PER_CYCLE", "50")
    big = "x" * 200
    _stub_provider(monkeypatch, big)

    with llm_budget.cycle_scope() as budget:
        with pytest.raises(llm_budget.LLMBudgetExceeded) as exc:
            llm_provider.call_llm(big, big)
        assert exc.value.reason == "tokens_exhausted"

    assert budget.aborted_reason == "tokens_exhausted"
    assert budget.tokens >= 50


# ---------------------------------------------------------------------------
# Per-provider failure cap (also acts as circuit breaker)
# ---------------------------------------------------------------------------


def test_provider_failures_cap_raises_with_reason_and_provider(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE", "2")
    # Return empty string each time — call_llm classifies empty as a provider failure.
    _stub_provider(monkeypatch, "")

    with llm_budget.cycle_scope() as budget:
        # First two failures are recorded but don't raise (counter goes 1, 2).
        # The second one hits the cap and raises during record_failure.
        llm_provider.call_llm("p", "t")  # failure 1
        with pytest.raises(llm_budget.LLMBudgetExceeded) as exc:
            llm_provider.call_llm("p", "t")  # failure 2 -> hits cap
        assert exc.value.reason == "provider_failures_exhausted"
        assert exc.value.provider == "google"

    assert budget.provider_failures.get("google", 0) >= 2


def test_circuit_breaker_blocks_further_calls_to_same_provider(monkeypatch):
    """Once a provider's failure cap is hit, further calls to it must be
    blocked at check_before_call without contacting the provider again."""
    monkeypatch.setenv("MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE", "1")
    calls = _stub_provider(monkeypatch, "")

    with llm_budget.cycle_scope():
        with pytest.raises(llm_budget.LLMBudgetExceeded):
            llm_provider.call_llm("p", "t")  # immediate fail → breaker opens
        # Next call: check_before_call should raise before invoking the provider.
        with pytest.raises(llm_budget.LLMBudgetExceeded) as exc:
            llm_provider.call_llm("p", "t")
        assert exc.value.reason == "provider_failures_exhausted"
        assert exc.value.provider == "google"

    # Provider was called exactly once across both attempts.
    assert calls[0] == 1


# ---------------------------------------------------------------------------
# service.run_cycle surfaces budget telemetry
# ---------------------------------------------------------------------------


def test_run_cycle_includes_budget_snapshot(tmp_path, monkeypatch):
    """When run_cycle completes without hitting any cap, the result dict
    still includes a budget snapshot with aborted=False."""
    from memorymaster.service import MemoryService

    db_path = tmp_path / "mm.db"
    svc = MemoryService(db_target=db_path, workspace_root=tmp_path)
    svc.init_db()
    result = svc.run_cycle()
    assert "budget" in result
    budget = result["budget"]
    assert budget["aborted"] is False
    assert budget["calls"] == 0  # no LLM calls happen in an empty-DB cycle


def test_run_cycle_surfaces_abort_when_calls_cap_hit(tmp_path, monkeypatch, caplog):
    """If something inside run_cycle calls call_llm beyond the cap, the
    result dict's budget block reports aborted=True with the reason —
    and the abort is logged at WARNING."""
    from memorymaster.service import MemoryService

    # Force any LLM call to fire — and inject an extractor-like usage by
    # directly invoking call_llm inside a fake stage. We can't easily make
    # the production extractor stages call the LLM in a test, so simulate
    # mid-cycle: patch one of the run_cycle stages to invoke call_llm.
    from memorymaster.govern.jobs import extractor

    monkeypatch.setenv("MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE", "1")
    _stub_provider(monkeypatch, "ok")

    real_run = extractor.run

    def chatty_run(store, *args, **kwargs):
        # Two calls inside one stage; second one should raise.
        llm_provider.call_llm("a", "b")
        llm_provider.call_llm("a", "b")  # over cap → raises
        return real_run(store, *args, **kwargs)

    monkeypatch.setattr(extractor, "run", chatty_run)

    db_path = tmp_path / "mm.db"
    svc = MemoryService(db_target=db_path, workspace_root=tmp_path)
    svc.init_db()

    with caplog.at_level(logging.WARNING, logger="memorymaster.service"):
        result = svc.run_cycle()

    budget = result["budget"]
    assert budget["aborted"] is True
    assert budget["aborted_reason"] == "calls_exhausted"
    # Warning was emitted with the reason
    assert any(
        "aborted by llm budget" in rec.message and "calls_exhausted" in rec.message
        for rec in caplog.records
    )
