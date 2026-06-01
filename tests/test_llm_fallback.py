"""Tests for the LLM provider fallback chain.

Covers `call_llm` behavior when MEMORYMASTER_LLM_FALLBACK_PROVIDER is set.
All provider functions are monkeypatched — no real network I/O.

See roadmap item 11.1 and claim 11907 (silent quota-exhausted state).
"""
from __future__ import annotations

import os
from time import perf_counter

import pytest

from memorymaster import llm_provider


@pytest.fixture(autouse=True)
def _reset_env_and_stats(monkeypatch):
    """Scrub every env var the fallback logic reads and reset counters."""
    for key in (
        "MEMORYMASTER_LLM_PROVIDER",
        "MEMORYMASTER_LLM_MODEL",
        "MEMORYMASTER_LLM_FALLBACK_PROVIDER",
        "MEMORYMASTER_LLM_FALLBACK_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    llm_provider.reset_fallback_stats()
    yield
    llm_provider.reset_fallback_stats()


def _install_providers(monkeypatch, primary_fn, fallback_fn):
    """Replace _PROVIDERS with two fake callables under stable names."""
    fake_providers = {"fake_primary": primary_fn, "fake_fallback": fallback_fn}
    monkeypatch.setattr(llm_provider, "_PROVIDERS", fake_providers)


def test_case_a_primary_ok_fallback_never_called(monkeypatch):
    """Primary returns 'ok' → fallback never called, primary_ok=1, fired=0."""
    fallback_calls: list[tuple[str, str]] = []

    def primary(prompt: str, text: str) -> str:
        return "ok"

    def fallback(prompt: str, text: str) -> str:
        fallback_calls.append((prompt, text))
        return "SHOULD NOT BE CALLED"

    _install_providers(monkeypatch, primary, fallback)
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "fake_primary")
    monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "fake_fallback")

    start = perf_counter()
    result = llm_provider.call_llm("sys", "user")
    elapsed = perf_counter() - start

    assert result == "ok"
    assert fallback_calls == []
    stats = llm_provider.get_fallback_stats()
    assert stats == {"attempts": 1, "fired": 0, "primary_ok": 1}
    assert elapsed < 1.0  # sanity: no hanging


def test_case_b_primary_empty_triggers_fallback(monkeypatch):
    """Primary returns '' → fallback called, fired=1."""
    def primary(prompt: str, text: str) -> str:
        return ""

    def fallback(prompt: str, text: str) -> str:
        return "fallback-response"

    _install_providers(monkeypatch, primary, fallback)
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "fake_primary")
    monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "fake_fallback")

    result = llm_provider.call_llm("sys", "user")

    assert result == "fallback-response"
    stats = llm_provider.get_fallback_stats()
    assert stats["attempts"] == 1
    assert stats["fired"] == 1
    assert stats["primary_ok"] == 0


def test_case_c_primary_quota_body_triggers_fallback(monkeypatch):
    """Primary returns body containing RESOURCE_EXHAUSTED → fallback called."""
    quota_body = (
        '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", '
        '"message": "You exceeded your current quota"}}'
    )

    def primary(prompt: str, text: str) -> str:
        return quota_body

    def fallback(prompt: str, text: str) -> str:
        return "fallback-ok"

    _install_providers(monkeypatch, primary, fallback)
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "fake_primary")
    monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "fake_fallback")

    # Sanity check the regex directly first.
    assert llm_provider._QUOTA_EXHAUSTED_RE.search(quota_body)

    result = llm_provider.call_llm("sys", "user")

    assert result == "fallback-ok"
    stats = llm_provider.get_fallback_stats()
    assert stats["fired"] == 1
    assert stats["primary_ok"] == 0


def test_case_d_no_fallback_env_returns_primary_empty(monkeypatch):
    """Fallback env unset → primary's empty response passes through, no fallback call."""
    fallback_calls: list[tuple[str, str]] = []

    def primary(prompt: str, text: str) -> str:
        return ""

    def fallback(prompt: str, text: str) -> str:
        fallback_calls.append((prompt, text))
        return "unreachable"

    _install_providers(monkeypatch, primary, fallback)
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "fake_primary")
    # MEMORYMASTER_LLM_FALLBACK_PROVIDER explicitly NOT set.

    result = llm_provider.call_llm("sys", "user")

    assert result == ""
    assert fallback_calls == []
    stats = llm_provider.get_fallback_stats()
    assert stats["attempts"] == 1
    assert stats["fired"] == 0
    assert stats["primary_ok"] == 0


def test_case_e_both_fail_no_crash_returns_primary(monkeypatch):
    """Both fail → return primary empty, fired=1 but no crash."""
    def primary(prompt: str, text: str) -> str:
        return ""

    def fallback(prompt: str, text: str) -> str:
        return ""

    _install_providers(monkeypatch, primary, fallback)
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "fake_primary")
    monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "fake_fallback")

    result = llm_provider.call_llm("sys", "user")

    assert result == ""
    stats = llm_provider.get_fallback_stats()
    assert stats["attempts"] == 1
    assert stats["fired"] == 1  # fallback was attempted even though it also failed
    assert stats["primary_ok"] == 0


def test_case_f_fallback_model_is_honored_and_restored(monkeypatch):
    """MEMORYMASTER_LLM_FALLBACK_MODEL is honored during fallback, restored after.

    The model override is delivered to providers via ``llm_provider._env``
    (a contextvar override) rather than by mutating ``os.environ`` — every real
    provider (_call_google/_call_openai/...) reads its model through ``_env``,
    so the stubs read it the same way. Mutating os.environ would let a
    concurrent thread observe the wrong model mid-fallback, which is the bug
    this contract guards against.
    """
    observed_models: dict[str, str | None] = {}
    observed_os_environ: dict[str, str | None] = {}

    def primary(prompt: str, text: str) -> str:
        observed_models["primary"] = llm_provider._env("MEMORYMASTER_LLM_MODEL")
        return ""  # Force fallback.

    def fallback(prompt: str, text: str) -> str:
        observed_models["fallback"] = llm_provider._env("MEMORYMASTER_LLM_MODEL")
        # os.environ must NOT carry the fallback model — a concurrent thread
        # reading it directly must still see the primary model.
        observed_os_environ["fallback"] = os.environ.get("MEMORYMASTER_LLM_MODEL")
        return "fb-ok"

    _install_providers(monkeypatch, primary, fallback)
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "fake_primary")
    monkeypatch.setenv("MEMORYMASTER_LLM_MODEL", "gemini-3.1-flash-lite-preview")
    monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "fake_fallback")
    monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_MODEL", "gemma4:e4b")

    result = llm_provider.call_llm("sys", "user")

    assert result == "fb-ok"
    # Primary saw the primary model.
    assert observed_models["primary"] == "gemini-3.1-flash-lite-preview"
    # Fallback saw the fallback model (override applied via contextvar).
    assert observed_models["fallback"] == "gemma4:e4b"
    # os.environ was never mutated — concurrent threads stay correct.
    assert observed_os_environ["fallback"] == "gemini-3.1-flash-lite-preview"
    # After call_llm returns, the primary model is still the effective one.
    assert llm_provider._env("MEMORYMASTER_LLM_MODEL") == "gemini-3.1-flash-lite-preview"
    assert os.environ.get("MEMORYMASTER_LLM_MODEL") == "gemini-3.1-flash-lite-preview"


# ---------------------------------------------------------------------------
# Regex defensiveness checks (claim 11907 — false-positive avoidance)
# ---------------------------------------------------------------------------


def test_quota_regex_does_not_match_legitimate_quota_mentions():
    """Plain prose mentioning 'quota' MUST NOT trigger the fallback."""
    benign_responses = [
        "The user asked about their sales quota for Q3.",
        "Quota management is a common concern in SaaS pricing.",
        "Here are three entities: Alice, Bob, a quota.",
        '{"entities": [{"name": "disk quota policy", "type": "concept"}]}',
    ]
    for body in benign_responses:
        assert not llm_provider._looks_like_quota_error(body), (
            f"false-positive on benign body: {body!r}"
        )


def test_quota_regex_matches_real_error_shapes():
    """Real quota-error bodies from Gemini/OpenAI-style responses MUST match."""
    real_error_bodies = [
        '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}',
        'HTTP 429 Too Many Requests',
        '{"error": "quota exceeded for model gemini-3.1-flash-lite-preview"}',
        'rate limit exceeded; please retry in 60s',
    ]
    for body in real_error_bodies:
        assert llm_provider._looks_like_quota_error(body), (
            f"false-negative on real quota body: {body!r}"
        )
