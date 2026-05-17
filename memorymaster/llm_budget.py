"""Per-cycle LLM budget caps with reason-coded hard stops.

A cycle scope tracks LLM call count, estimated tokens, and per-provider
failures. When any cap is exceeded, the next ``llm_provider.call_llm``
raises ``LLMBudgetExceeded`` with a reason code so callers can record
the abort visibly instead of silently overspending.

Caps are read from env vars at scope-entry time (any change requires a
new scope to take effect):

- ``MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE``       — hard cap on total calls
- ``MEMORYMASTER_MAX_TOKENS_PER_CYCLE``          — hard cap on summed estimated tokens
- ``MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE`` — per-provider failure ceiling
                                                    (also acts as circuit breaker)

A value of ``0`` (default) means "unlimited" for that axis — preserves
backwards compatibility when env vars are unset.

Usage::

    from memorymaster import llm_budget

    with llm_budget.cycle_scope() as budget:
        ...
        try:
            response = llm_provider.call_llm(prompt, text)
        except llm_budget.LLMBudgetExceeded as exc:
            # exc.reason in {"calls_exhausted", "tokens_exhausted",
            #                "provider_failures_exhausted"}
            # exc.provider is set only for provider-failures reason.
            ...
        ...
    snapshot = budget.snapshot()  # totals after the scope
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator


# ---------------------------------------------------------------------------
# Exception + dataclass
# ---------------------------------------------------------------------------


class LLMBudgetExceeded(Exception):
    """Raised by ``call_llm`` when a per-cycle budget cap is hit.

    Attributes:
        reason: one of ``"calls_exhausted"``, ``"tokens_exhausted"``,
            ``"provider_failures_exhausted"``.
        provider: provider name (set only for the failures reason).
    """

    def __init__(self, reason: str, provider: str | None = None) -> None:
        self.reason = reason
        self.provider = provider
        suffix = f" provider={provider}" if provider else ""
        super().__init__(f"llm budget exceeded: reason={reason}{suffix}")


@dataclass
class CycleBudget:
    """Per-cycle counters and limits. Lives for the duration of one scope.

    Limits of 0 mean unlimited. ``aborted_reason`` is set the first time
    a cap is hit (subsequent overruns don't overwrite the original reason).
    """

    max_calls: int = 0
    max_tokens: int = 0
    max_provider_failures: int = 0
    calls: int = 0
    tokens: int = 0
    provider_failures: dict[str, int] = field(default_factory=dict)
    aborted_reason: str | None = None
    aborted_provider: str | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "max_calls": self.max_calls,
            "max_tokens": self.max_tokens,
            "max_provider_failures": self.max_provider_failures,
            "calls": self.calls,
            "tokens": self.tokens,
            "provider_failures": dict(self.provider_failures),
            "aborted_reason": self.aborted_reason,
            "aborted_provider": self.aborted_provider,
        }


# ---------------------------------------------------------------------------
# Context variable
# ---------------------------------------------------------------------------


_current: ContextVar[CycleBudget | None] = ContextVar(
    "memorymaster_llm_cycle_budget", default=None
)


def _read_int_env(name: str) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _new_from_env() -> CycleBudget:
    return CycleBudget(
        max_calls=_read_int_env("MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE"),
        max_tokens=_read_int_env("MEMORYMASTER_MAX_TOKENS_PER_CYCLE"),
        max_provider_failures=_read_int_env("MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE"),
    )


@contextmanager
def cycle_scope() -> Iterator[CycleBudget]:
    """Open a new per-cycle budget scope. Yields the tracker; auto-cleans on exit."""
    budget = _new_from_env()
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)


def get_current() -> CycleBudget | None:
    """Return the active cycle budget, or None if no scope is open."""
    return _current.get()


# ---------------------------------------------------------------------------
# Enforcement helpers — invoked from llm_provider.call_llm
# ---------------------------------------------------------------------------


def _abort(budget: CycleBudget, reason: str, provider: str | None = None) -> None:
    """Record the first abort reason and raise."""
    if budget.aborted_reason is None:
        budget.aborted_reason = reason
        budget.aborted_provider = provider
    raise LLMBudgetExceeded(reason, provider)


def estimate_tokens(*parts: str) -> int:
    """Rough char/4 estimator. Sufficient for cap accounting; not for billing."""
    total = sum(len(p) for p in parts if p)
    return (total + 3) // 4


def check_before_call(provider: str) -> None:
    """Raise if the calls cap is already at its ceiling, or this provider's
    failure breaker is open. Called before contacting the provider."""
    budget = get_current()
    if budget is None:
        return
    if budget.max_calls and budget.calls >= budget.max_calls:
        _abort(budget, "calls_exhausted")
    if budget.max_provider_failures:
        if budget.provider_failures.get(provider, 0) >= budget.max_provider_failures:
            _abort(budget, "provider_failures_exhausted", provider)


def record_call(provider: str, *, tokens: int = 0) -> None:
    """Record a successful (or attempted) call. Raises if tokens cap is hit."""
    budget = get_current()
    if budget is None:
        return
    budget.calls += 1
    budget.tokens += max(0, tokens)
    if budget.max_tokens and budget.tokens >= budget.max_tokens:
        _abort(budget, "tokens_exhausted")


def record_failure(provider: str) -> None:
    """Increment per-provider failure counter. Raises if breaker hits limit."""
    budget = get_current()
    if budget is None:
        return
    new_count = budget.provider_failures.get(provider, 0) + 1
    budget.provider_failures[provider] = new_count
    if budget.max_provider_failures and new_count >= budget.max_provider_failures:
        _abort(budget, "provider_failures_exhausted", provider)
