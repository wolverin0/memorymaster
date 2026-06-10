"""Tests for v3.10 W1 — closets stream wired into context_hook.recall."""
from __future__ import annotations

import pytest

from memorymaster.recall import context_hook


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in [
        "MEMORYMASTER_RECALL_CLOSETS",
        "MEMORYMASTER_RECALL_W_CLOSETS",
        "MEMORYMASTER_RECALL_TWO_PASS",
        "MEMORYMASTER_RECALL_TWO_PASS_USE_EDGES",
    ]:
        monkeypatch.delenv(k, raising=False)


def test_closets_disabled_by_default():
    assert context_hook._closets_enabled() is False


def test_closets_enabled_via_env(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_CLOSETS", "1")
    assert context_hook._closets_enabled() is True


def test_closets_disabled_for_falsy_values(monkeypatch):
    for v in ["0", "false", "False", "no", "off", ""]:
        monkeypatch.setenv("MEMORYMASTER_RECALL_CLOSETS", v)
        assert context_hook._closets_enabled() is False, f"failed for {v!r}"


def test_w_closets_default_is_zero():
    assert context_hook._RECALL_WEIGHT_DEFAULTS["W_CLOSETS"] == 0.0


def test_w_closets_override(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_CLOSETS", "0.4")
    assert context_hook._recall_weight("W_CLOSETS") == 0.4


def test_two_pass_use_edges_disabled_by_default():
    assert context_hook._two_pass_use_edges() is False


def test_two_pass_use_edges_enabled_via_env(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS_USE_EDGES", "1")
    assert context_hook._two_pass_use_edges() is True
