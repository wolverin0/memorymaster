"""Tests for v3.9.0 F5 — two-pass entity-fanout retrieval.

Gated by MEMORYMASTER_RECALL_TWO_PASS=1 + MEMORYMASTER_RECALL_W_TWO_PASS > 0.
Default keeps ranking bit-identical. The DB walker is defensive: missing
tables → []. Recall regression suite must still be green.
"""
from __future__ import annotations

import pytest

from memorymaster.recall import context_hook


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in [
        "MEMORYMASTER_RECALL_TWO_PASS",
        "MEMORYMASTER_RECALL_TWO_PASS_MAX",
        "MEMORYMASTER_RECALL_W_TWO_PASS",
    ]:
        monkeypatch.delenv(k, raising=False)


def test_two_pass_disabled_by_default():
    assert context_hook._two_pass_enabled() is False


def test_two_pass_enabled_via_env(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS", "1")
    assert context_hook._two_pass_enabled() is True


def test_two_pass_disabled_for_falsy_values(monkeypatch):
    for v in ["0", "false", "False", "no", "off", ""]:
        monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS", v)
        assert context_hook._two_pass_enabled() is False, f"failed for {v!r}"


def test_two_pass_max_default():
    assert context_hook._two_pass_max_neighbors() == 20


def test_two_pass_max_override(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS_MAX", "50")
    assert context_hook._two_pass_max_neighbors() == 50


def test_two_pass_max_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_TWO_PASS_MAX", "not-a-number")
    assert context_hook._two_pass_max_neighbors() == 20


def test_w_two_pass_default_is_zero():
    assert context_hook._RECALL_WEIGHT_DEFAULTS["W_TWO_PASS"] == 0.0


def test_w_two_pass_override(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_TWO_PASS", "0.25")
    assert context_hook._recall_weight("W_TWO_PASS") == 0.25


def test_neighbor_ids_empty_seeds_returns_empty():
    """Defensive: no seeds → no walk."""
    assert context_hook._two_pass_neighbor_ids(None, [], set()) == []


def test_neighbor_ids_no_conn_returns_empty():
    """Defensive: store has no _conn or conn → []."""

    class _StoreNoConn:
        pass

    assert context_hook._two_pass_neighbor_ids(_StoreNoConn(), [1, 2, 3], set()) == []


def test_neighbor_ids_db_error_returns_empty():
    """If the DB raises (e.g. missing claim_entities table) → silent [] not crash."""

    class _BadConn:
        def execute(self, *args, **kwargs):
            raise RuntimeError("table claim_entities does not exist")

    class _Store:
        _conn = _BadConn()

    assert context_hook._two_pass_neighbor_ids(_Store(), [1, 2, 3], set()) == []
