"""Tests for v3.9.0 F1 — claim_type-aware ranking in context_hook._relevance.

The boost is opt-in via MEMORYMASTER_RECALL_W_CLAIM_TYPE > 0. Default 0.0
preserves bit-identical ranking. When enabled and the query is classified
into a known claim_type, rows whose claim.claim_type matches get a
(1 + w_claim_type) multiplier on the final base score.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from memorymaster.recall import context_hook


def _claim(cid: int, text: str, claim_type: str) -> MagicMock:
    c = MagicMock()
    c.id = cid
    c.text = text
    c.claim_type = claim_type
    c.scope = "global"
    c.confidence = 0.5
    return c


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in [
        "MEMORYMASTER_RECALL_W_CLAIM_TYPE",
        "MEMORYMASTER_RECALL_W_LEXICAL",
        "MEMORYMASTER_RECALL_W_FRESHNESS",
        "MEMORYMASTER_RECALL_W_GRAPH",
        "MEMORYMASTER_RECALL_W_VECTOR",
        "MEMORYMASTER_RECALL_W_ENTITY",
        "MEMORYMASTER_RECALL_W_VERBATIM",
        "MEMORYMASTER_RECALL_SCOPE_BOOST",
    ]:
        monkeypatch.delenv(k, raising=False)


def test_default_weight_is_zero():
    """When the env var is unset, the W_CLAIM_TYPE default must be 0.0."""
    assert context_hook._RECALL_WEIGHT_DEFAULTS["W_CLAIM_TYPE"] == 0.0


def test_weight_overrides_via_env(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_CLAIM_TYPE", "0.4")
    assert context_hook._recall_weight("W_CLAIM_TYPE") == 0.4


def test_classify_observation_returns_decision_for_decision_query():
    """Sanity that the classifier we lean on identifies known triggers."""
    assert context_hook.classify_observation("we decided to use postgres") == "decision"


def test_classify_observation_returns_constraint():
    """`require` is a constraint trigger that doesn't double-match preference."""
    assert context_hook.classify_observation("we require parameterized queries") == "constraint"


def test_classify_observation_returns_none_for_neutral_query():
    """A bare keyword query without trigger words should stay un-classified."""
    assert context_hook.classify_observation("postgres version") is None


# The actual scoring closure is built inside recall(), so we can't unit-test
# the closure directly without invoking the whole pipeline. The behavioural
# proof lives in the regression check below: query the env var, the default,
# and the classify path. The end-to-end recall@5 lift (or null) is measured
# in the live N=953 eval and recorded in artifacts/.


def test_pattern_list_includes_canonical_kinds():
    """The classifier emits one of: preference, decision, constraint, fact, event, commitment.
    F1 boost only fires when the row's claim_type matches one of these strings."""
    kinds = {t for _, t in context_hook.OBSERVATION_PATTERNS}
    assert "decision" in kinds
    assert "constraint" in kinds
    assert "preference" in kinds
    assert "event" in kinds
    assert "commitment" in kinds
    assert "fact" in kinds
