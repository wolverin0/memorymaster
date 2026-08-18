"""R8 — ``_llm_rerank_enabled`` must fail CLOSED.

The gate reads a circuit breaker: ``rerank_temporarily_disabled()`` says the
LLM reranker has been tripped and must not be called. If that import fails the
breaker is unreadable — which is the strongest possible reason NOT to call a
paid provider. The function nevertheless returned ``True``, reporting the
feature ENABLED, while every other branch in the same function fails closed.
"""
from __future__ import annotations

import builtins

from memorymaster.core import service


def _force_gate_prerequisites(monkeypatch) -> None:
    """Get past the two cheap gates so the try/except branch is the decider."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        service, "get_config", lambda: type("C", (), {"llm_rerank": True})()
    )


def test_rerank_gate_fails_closed_when_the_breaker_is_unreadable(monkeypatch) -> None:
    _force_gate_prerequisites(monkeypatch)
    real_import = builtins.__import__

    def _explode(name, *args, **kwargs):
        if name == "memorymaster.recall.llm_rerank":
            raise ImportError("breaker module is unreadable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _explode)

    assert service._llm_rerank_enabled() is False, (
        "an unreadable circuit breaker reported the reranker ENABLED"
    )


def test_rerank_gate_still_honours_a_readable_breaker(monkeypatch) -> None:
    """The fix must not turn the gate into a constant False."""
    _force_gate_prerequisites(monkeypatch)
    from memorymaster.recall import llm_rerank

    monkeypatch.setattr(llm_rerank, "rerank_temporarily_disabled", lambda: False)
    assert service._llm_rerank_enabled() is True

    monkeypatch.setattr(llm_rerank, "rerank_temporarily_disabled", lambda: True)
    assert service._llm_rerank_enabled() is False
