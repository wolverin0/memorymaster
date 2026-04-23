"""Regression tests for the MEMORYMASTER_POLICY_MODE env-var switch.

The steward cycle's ``policy`` block was reporting ``considered=0`` because
six callers hardcode ``policy_mode='legacy'`` as the default, and legacy
is an explicit no-op stub. This env var lets operators opt into the real
``cadence`` selector at runtime without editing any caller.

See ``artifacts/session-handoff-2026-04-23.md`` and the diagnostic claim
for the full story.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from memorymaster.policy import POLICY_MODES, select_revalidation_candidates


class _FakeStore:
    """Minimal store stub exposing only ``list_claims`` — returns a fixed
    pool the cadence branch will iterate over.
    """

    def __init__(self, claims):
        self._claims = claims

    def list_claims(self, **_kw):
        return self._claims


@pytest.fixture
def empty_store():
    return _FakeStore([])


def test_legacy_default_with_no_env(monkeypatch, empty_store):
    """With no env var set, mode=legacy keeps the no-op stub behavior."""
    monkeypatch.delenv("MEMORYMASTER_POLICY_MODE", raising=False)
    result = select_revalidation_candidates(empty_store, mode="legacy")
    assert result.mode == "legacy"
    assert result.considered == 0
    assert result.due == 0
    assert result.selected == []


def test_env_cadence_promotes_legacy_default(monkeypatch, empty_store):
    """Env=cadence flips the default-legacy path to the cadence selector."""
    monkeypatch.setenv("MEMORYMASTER_POLICY_MODE", "cadence")
    result = select_revalidation_candidates(empty_store, mode="legacy")
    assert result.mode == "cadence"
    # Empty store → cadence still returns zero, but via the REAL path.
    # The key contract is ``mode`` flipped to cadence.
    assert result.considered == 0


def test_env_invalid_value_is_ignored(monkeypatch, empty_store):
    """Unknown env value must not break anything — falls back to legacy."""
    monkeypatch.setenv("MEMORYMASTER_POLICY_MODE", "not-a-real-mode")
    result = select_revalidation_candidates(empty_store, mode="legacy")
    assert result.mode == "legacy"
    assert result.considered == 0


def test_env_does_not_clobber_explicit_cadence(monkeypatch, empty_store):
    """If caller passes mode=cadence explicitly, env must not downgrade it."""
    monkeypatch.setenv("MEMORYMASTER_POLICY_MODE", "legacy")
    result = select_revalidation_candidates(empty_store, mode="cadence")
    # Caller's choice wins; env only promotes legacy defaults.
    assert result.mode == "cadence"


def test_env_case_insensitive(monkeypatch, empty_store):
    """MEMORYMASTER_POLICY_MODE accepts upper/lower/mixed case."""
    for value in ("CADENCE", "Cadence", " cadence "):
        monkeypatch.setenv("MEMORYMASTER_POLICY_MODE", value)
        result = select_revalidation_candidates(empty_store, mode="legacy")
        assert result.mode == "cadence", f"failed for {value!r}"


def test_unknown_explicit_mode_still_raises(empty_store):
    """Invalid explicit mode from a caller is still a hard error."""
    with pytest.raises(ValueError, match="Unknown policy mode"):
        select_revalidation_candidates(empty_store, mode="bogus")


def test_policy_modes_tuple_unchanged():
    """Regression guard: the valid-modes tuple must not silently expand."""
    assert POLICY_MODES == ("legacy", "cadence")
