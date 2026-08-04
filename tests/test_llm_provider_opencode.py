from __future__ import annotations

from types import SimpleNamespace

import pytest

from memorymaster.core import llm_provider
from memorymaster.core import opencode_client


@pytest.fixture(autouse=True)
def _clean_provider(monkeypatch: pytest.MonkeyPatch):
    for key in (
        "MEMORYMASTER_LLM_PROVIDER",
        "MEMORYMASTER_LLM_MODEL",
        "MEMORYMASTER_LLM_REASONING_EFFORT",
        "MEMORYMASTER_OPENCODE_TIMEOUT",
    ):
        monkeypatch.delenv(key, raising=False)
    llm_provider._OPENCODE_CLIENTS.clear()
    yield
    llm_provider._OPENCODE_CLIENTS.clear()


def test_call_llm_dispatches_to_cached_opencode_oauth_client(monkeypatch) -> None:
    created: list[dict] = []
    prompts: list[str] = []

    class FakeJudge:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def complete(self, prompt: str):
            prompts.append(prompt)
            return SimpleNamespace(text="structured result")

    monkeypatch.setattr(opencode_client, "OpenCodeClient", FakeJudge)
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "opencode")
    monkeypatch.setenv("MEMORYMASTER_LLM_MODEL", "openai/gpt-5.6-terra")
    monkeypatch.setenv("MEMORYMASTER_LLM_REASONING_EFFORT", "high")

    assert llm_provider.call_llm("system", "evidence") == "structured result"
    assert llm_provider.call_llm("system", "second") == "structured result"
    assert len(created) == 1
    assert created[0]["model"] == "openai/gpt-5.6-terra"
    assert created[0]["effort"] == "high"
    assert prompts == ["system\n\nevidence", "system\n\nsecond"]


def test_opencode_failure_preserves_empty_provider_contract(monkeypatch) -> None:
    class FailingJudge:
        def __init__(self, **kwargs):
            pass

        def complete(self, prompt: str):
            raise opencode_client.OpenCodeClientError("timeout", "private detail")

    monkeypatch.setattr(opencode_client, "OpenCodeClient", FailingJudge)
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "opencode")

    assert llm_provider.call_llm("system", "evidence") == ""


def test_invalid_opencode_timeout_uses_bounded_default(monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_OPENCODE_TIMEOUT", "not-a-number")
    assert llm_provider._opencode_timeout() == 180
