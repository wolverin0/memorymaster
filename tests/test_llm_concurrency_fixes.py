"""Regression tests for the llm-concurrency cluster fixes.

Each test encodes WHY the fix matters (intent), not just the mechanism:

1. env-rotation must NOT cool a HEALTHY key on an empty-200 success — doing so
   across a batch cools every key and makes get_key falsely sleep on
   "all keys rate-limited".
2. the fallback model swap must NOT mutate os.environ — a concurrent thread
   reading MEMORYMASTER_LLM_MODEL mid-fallback would pick the wrong model.
3. the rerank judge env must NOT mutate process-global env or clear the shared
   key-rotator cache — that poisons concurrent call_llm invocations.
4. the per-cycle LLM budget cap must be enforced for a call_llm that runs inside
   a ThreadPoolExecutor worker under an open cycle_scope — contextvars are not
   inherited by pool workers unless explicitly propagated.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from memorymaster import llm_budget, llm_provider


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for key in (
        "MEMORYMASTER_LLM_PROVIDER",
        "MEMORYMASTER_LLM_MODEL",
        "MEMORYMASTER_LLM_FALLBACK_PROVIDER",
        "MEMORYMASTER_LLM_FALLBACK_MODEL",
        "MEMORYMASTER_LLM_KEY_ROTATION",
        "MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE",
    ):
        monkeypatch.delenv(key, raising=False)
    llm_provider.reset_fallback_stats()
    yield
    llm_provider.reset_fallback_stats()


# ---------------------------------------------------------------------------
# Finding 1 — empty-200 must not cool a healthy env-rotation key
# ---------------------------------------------------------------------------


def test_empty_200_does_not_rate_limit_healthy_key(monkeypatch):
    """An empty-but-successful (HTTP 200) response leaves the key uncooled.

    WHY: cooling a healthy key on every empty response cools the entire key set
    across a batch, which then makes get_key sleep and falsely report "all keys
    rate-limited" even though no key ever hit a 429.
    """
    from memorymaster.llm_steward import KeyRotator

    rotator = KeyRotator(keys=["k1", "k2", "k3"])
    monkeypatch.setattr(llm_provider, "_get_google_env_rotator", lambda: rotator)

    # Simulate empty-200: _http_post returns "" and writes NO http_status.
    def fake_post(url, payload, extractor, **kwargs):
        sink = kwargs.get("status_sink")
        if sink is not None:
            sink["http_status"] = None  # success-but-empty
        return ""

    monkeypatch.setattr(llm_provider, "_http_post", fake_post)

    result = llm_provider._call_google_with_env_rotation("m", {})
    assert result == ""
    # No key was placed on cooldown: every key is still available.
    assert rotator.available_key_count == rotator.key_count


def test_real_429_still_cools_the_key(monkeypatch):
    """A genuine HTTP 429 still cools the offending key (behavior preserved)."""
    from memorymaster.llm_steward import KeyRotator

    rotator = KeyRotator(keys=["k1", "k2"])
    monkeypatch.setattr(llm_provider, "_get_google_env_rotator", lambda: rotator)

    def fake_post(url, payload, extractor, **kwargs):
        sink = kwargs.get("status_sink")
        if sink is not None:
            sink["http_status"] = 429
        return ""

    monkeypatch.setattr(llm_provider, "_http_post", fake_post)

    llm_provider._call_google_with_env_rotation("m", {})
    # Both keys got a 429 → both cooled.
    assert rotator.available_key_count == 0


# ---------------------------------------------------------------------------
# Finding 2 — fallback model swap must not mutate os.environ
# ---------------------------------------------------------------------------


def test_fallback_model_swap_does_not_touch_os_environ(monkeypatch):
    """During fallback the model override is applied via contextvars, not env.

    WHY: a concurrent thread reading os.environ['MEMORYMASTER_LLM_MODEL'] while
    the fallback call is in flight must NOT observe the fallback model.
    """
    monkeypatch.setenv("MEMORYMASTER_LLM_MODEL", "primary-model")
    monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_MODEL", "fallback-model")

    observed_in_fallback: dict[str, str] = {}
    env_during_fallback: dict[str, str | None] = {}

    def primary(prompt, text):
        return ""  # force fallback

    def fallback(prompt, text):
        # What the provider sees via _env (should be the fallback model)...
        observed_in_fallback["via_env_helper"] = llm_provider._env("MEMORYMASTER_LLM_MODEL")
        # ...but os.environ must be untouched (what a concurrent thread sees).
        env_during_fallback["os_environ"] = os.environ.get("MEMORYMASTER_LLM_MODEL")
        return "ok"

    monkeypatch.setattr(
        llm_provider, "_PROVIDERS", {"p": primary, "f": fallback}
    )
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "p")
    monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "f")

    result = llm_provider.call_llm("sys", "user")
    assert result == "ok"
    assert observed_in_fallback["via_env_helper"] == "fallback-model"
    # os.environ never saw the fallback model — concurrent threads are safe.
    assert env_during_fallback["os_environ"] == "primary-model"
    assert os.environ["MEMORYMASTER_LLM_MODEL"] == "primary-model"


# ---------------------------------------------------------------------------
# Finding 3 — rerank judge env must not mutate global env / clear shared cache
# ---------------------------------------------------------------------------


def test_rerank_env_does_not_clear_shared_rotator_cache(monkeypatch):
    """Running a rerank judge call must NOT clear the shared rotator cache.

    WHY: clearing the cache mid-flight drops the rotation/cooldown state that a
    concurrent call_llm relies on, re-reading keys and double-spending quota.
    """
    from memorymaster import key_rotator, llm_rerank

    cleared = {"count": 0}
    real_clear = key_rotator.clear_cache

    def counting_clear():
        cleared["count"] += 1
        real_clear()

    monkeypatch.setattr(key_rotator, "clear_cache", counting_clear)

    captured: dict[str, str] = {}

    def fake_call_llm(prompt, text):
        captured["provider"] = llm_provider._env("MEMORYMASTER_LLM_PROVIDER")
        captured["model"] = llm_provider._env("MEMORYMASTER_LLM_MODEL")
        captured["rotation"] = llm_provider._env("MEMORYMASTER_LLM_KEY_ROTATION")
        captured["skip_rotator"] = llm_provider._SKIP_FILE_ROTATOR.get()
        return '[[1, 90]]'

    monkeypatch.setattr(llm_rerank, "call_llm", fake_call_llm)
    monkeypatch.setenv("MEMORYMASTER_LLM_RERANK_MIN_INTERVAL_SECONDS", "0")

    out = llm_rerank._call_rerank_judge("q", ["c1"], "judge-model")
    assert out == '[[1, 90]]'
    # The judge env was applied via contextvars, scoped to the call.
    assert captured["provider"] == "google"
    assert captured["model"] == "judge-model"
    assert captured["rotation"] == "0"
    assert captured["skip_rotator"] is True
    # The shared rotator cache was NEVER cleared as a side effect.
    assert cleared["count"] == 0


def test_rerank_env_does_not_mutate_os_environ(monkeypatch):
    """Provider/model/key-rotation are restored to os.environ after the call."""
    from memorymaster import llm_rerank

    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("MEMORYMASTER_LLM_MODEL", raising=False)
    monkeypatch.setenv("MEMORYMASTER_LLM_RERANK_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setattr(llm_rerank, "call_llm", lambda p, t: '[[1, 90]]')

    llm_rerank._call_rerank_judge("q", ["c1"], "judge-model")

    # os.environ untouched: the override lived only in the contextvar.
    assert os.environ["MEMORYMASTER_LLM_PROVIDER"] == "anthropic"
    assert "MEMORYMASTER_LLM_MODEL" not in os.environ


# ---------------------------------------------------------------------------
# Finding 4 — budget cap enforced inside a ThreadPoolExecutor worker
# ---------------------------------------------------------------------------


def test_budget_cap_enforced_in_pool_worker(monkeypatch):
    """call_llm inside a pool worker must respect the cycle's calls cap.

    WHY: contextvars are NOT inherited by ThreadPoolExecutor workers. Without
    propagating the context, get_current() is None in the worker, the cap is a
    no-op, and the steward silently overspends its per-cycle LLM budget.
    """
    import contextvars

    monkeypatch.setenv("MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE", "1")
    monkeypatch.setattr(llm_provider, "_PROVIDERS", {"x": lambda p, t: "ok"})
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "x")

    def two_calls_in_worker():
        # Two calls under a cap of 1: the SECOND must raise LLMBudgetExceeded.
        llm_provider.call_llm("a", "b")
        llm_provider.call_llm("a", "b")

    with llm_budget.cycle_scope():
        ctx = contextvars.copy_context()
        worker = ThreadPoolExecutor(max_workers=1)
        try:
            fut = worker.submit(ctx.run, two_calls_in_worker)
            with pytest.raises(llm_budget.LLMBudgetExceeded) as exc:
                fut.result(timeout=5)
        finally:
            worker.shutdown(wait=False)
    assert exc.value.reason == "calls_exhausted"


def test_budget_cap_bypassed_without_context_propagation(monkeypatch):
    """Control: WITHOUT context propagation the cap is silently bypassed.

    This is the bug the steward fix prevents — it documents the failure mode so
    the fix's value is anchored to the requirement, not the mechanism.
    """
    monkeypatch.setenv("MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE", "1")
    monkeypatch.setattr(llm_provider, "_PROVIDERS", {"x": lambda p, t: "ok"})
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "x")

    def two_calls_in_worker():
        llm_provider.call_llm("a", "b")
        llm_provider.call_llm("a", "b")  # would raise IF the cap were visible
        return "no-cap-seen"

    with llm_budget.cycle_scope():
        worker = ThreadPoolExecutor(max_workers=1)
        try:
            # submit WITHOUT ctx.run — worker does not see the scope.
            fut = worker.submit(two_calls_in_worker)
            assert fut.result(timeout=5) == "no-cap-seen"
        finally:
            worker.shutdown(wait=False)
