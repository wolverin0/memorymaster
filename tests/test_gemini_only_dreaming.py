"""The retired provider cannot be re-enabled by stale configuration."""

import pytest

from memorymaster.dreaming import providers
from memorymaster.surfaces import setup_hooks


@pytest.mark.parametrize("provider", ["glm", "zai", "zai-coding-plan", "opencode"])
def test_retired_consolidation_provider_fails_before_transport(monkeypatch, provider):
    monkeypatch.setenv("MEMORYMASTER_DREAM_CONSOLIDATE_PROVIDER", provider)
    with pytest.raises(providers.ProviderCallError, match="Gemini"):
        providers.create_dream_consolidator()


def test_retired_consolidator_is_not_exported():
    assert not hasattr(providers, "GLMConsolidator")


@pytest.mark.parametrize("provider", ["glm", "zai", "zai-coding-plan", "z.ai"])
def test_generic_transport_cannot_reenable_retired_provider(provider):
    with pytest.raises(providers.ProviderCallError, match="Retired provider"):
        providers._opencode_environment(provider)


@pytest.mark.parametrize("model", ["glm-5.2", "zai-coding-plan/glm-5.2", "openai/glm-5.2"])
def test_retired_model_cannot_hide_behind_another_provider(model):
    from memorymaster.core.opencode_client import OpenCodeClient, OpenCodeClientError

    with pytest.raises(OpenCodeClientError, match="Retired provider"):
        OpenCodeClient(model=model)
    with pytest.raises(providers.ProviderCallError, match="Retired provider"):
        providers.OpenCodeExtractor(model=model)


def test_readiness_uses_the_configured_gemini_cli(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_AGY_COMMAND", "custom-agy")
    monkeypatch.setattr(setup_hooks.shutil, "which", lambda name: name if name == "custom-agy" else None)
    assert setup_hooks._provider_readiness()["dream_consolidator"] is True
    monkeypatch.setattr(setup_hooks.shutil, "which", lambda name: name if name == "opencode" else None)
    assert setup_hooks._provider_readiness()["dream_consolidator"] is False
