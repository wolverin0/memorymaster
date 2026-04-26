"""Regression tests for the steward-cycle hook env wiring (v3.5.0 fix).

Bug: the deployed hook used `os.environ.setdefault("MEMORYMASTER_LLM_PROVIDER", ...)`,
which is a no-op when the inherited shell env already has the var set. After
switching the primary to claude_cli, an inherited `MEMORYMASTER_LLM_PROVIDER=google`
left the provider unchanged — but `MEMORYMASTER_LLM_MODEL` was set to the new
Claude model name, producing 50× HTTP 404 from the Gemini API per cycle before
the fallback chain saved it.

Fix: hook MUST use direct `os.environ["KEY"] = ...` assignment.

These tests assert against the SHIPPED template
(`memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`) so a
reverted edit fails CI before the broken hook reaches a user via
`memorymaster-setup`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "memorymaster"
    / "config_templates"
    / "hooks"
    / "memorymaster-steward-cycle.py"
)


@pytest.fixture(scope="module")
def template_text() -> str:
    assert TEMPLATE.exists(), f"steward hook template missing: {TEMPLATE}"
    return TEMPLATE.read_text(encoding="utf-8")


def test_template_does_not_use_setdefault_for_llm_provider(template_text: str) -> None:
    """If this fails, someone reverted the v3.5.0 hook env-assignment fix."""
    assert (
        'setdefault("MEMORYMASTER_LLM_PROVIDER"' not in template_text
    ), "steward hook must use os.environ['KEY'] = ... assignment, not setdefault"


def test_template_does_not_use_setdefault_for_llm_model(template_text: str) -> None:
    assert (
        'setdefault("MEMORYMASTER_LLM_MODEL"' not in template_text
    ), "steward hook must use os.environ['KEY'] = ... assignment, not setdefault"


@pytest.mark.parametrize(
    "key,expected_value",
    [
        ("MEMORYMASTER_LLM_PROVIDER", "claude_cli"),
        ("MEMORYMASTER_LLM_MODEL", "claude-haiku-4-5-20251001"),
        ("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "ollama"),
        ("MEMORYMASTER_LLM_FALLBACK_MODEL", "gemma4:e4b"),
    ],
)
def test_template_assigns_env_directly(
    template_text: str, key: str, expected_value: str
) -> None:
    """Each LLM env var must be set via direct assignment with the v3.5.0 default."""
    needle = f'os.environ["{key}"] = "{expected_value}"'
    assert (
        needle in template_text
    ), f"expected `{needle}` in template; either the assignment or value drifted"


def test_template_imports_only_from_stdlib_and_memorymaster(template_text: str) -> None:
    """The hook is invoked from cron/Task Scheduler; it can only depend on
    stdlib + the installed memorymaster package. A new third-party import here
    would silently break user installs that didn't pip install the dep."""
    forbidden = ("requests", "httpx", "aiohttp", "boto3")
    for pkg in forbidden:
        assert (
            f"import {pkg}" not in template_text
            and f"from {pkg}" not in template_text
        ), f"steward hook must not import {pkg} (would break installs missing the dep)"
