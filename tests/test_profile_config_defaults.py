"""Scheduled profile construction must honor the same policy as direct callers."""

from dataclasses import asdict

from memorymaster.profile.engine import ProfileConfig


def test_scheduled_defaults_match_direct_configuration(monkeypatch):
    import os

    for name in tuple(os.environ):
        if name.startswith("MEMORYMASTER_PROFILE_"):
            monkeypatch.delenv(name)
    assert asdict(ProfileConfig.from_env()) == asdict(ProfileConfig())


def test_explicit_projection_limits_still_win(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_PROFILE_TOKEN_BUDGET", "700")
    monkeypatch.setenv("MEMORYMASTER_PROFILE_MAX_FACTS", "25")
    config = ProfileConfig.from_env()
    assert config.token_budget == 700
    assert config.max_facts == 25
