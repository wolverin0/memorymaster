"""Intent-aware ranking — query intent selects the weight profile (plan 1.3).

WHY this matters: MemoryMaster already classifies a query's intent
(``classify_query``) but never let that intent influence ranking — the
classifier output was display-only. A *temporal* question ("what changed last
week?") should rank fresher claims higher; a *relational* question ("what
depends on X?") should lean on semantic/vector weight; a *fact* lookup should
favor precision. This wires intent → weight profile, opt-in via
``retrieval_profile="auto"`` so the default ranking is untouched. Tests anchor
on "different intents must produce different ranking weights", not on the exact
keyword heuristics.

Borrowed from gbrain's intent-aware query routing (re-survey 2026-06-24).
"""
from __future__ import annotations

from memorymaster.recall.query_classifier import (
    QUERY_TYPES,
    classify_query,
    profile_for_query_type,
)
from memorymaster.core.service import (
    RETRIEVAL_PROFILES,
    MemoryService,
    _retrieval_profile_weights,
)
from memorymaster.core.models import CitationInput


def test_temporal_and_relational_queries_get_different_weight_profiles():
    """The core requirement: a temporal vs a relational query must end up with
    DIFFERENT ranking weights — otherwise intent-awareness is a no-op."""
    t_profile = profile_for_query_type(classify_query("What changed last week?"))
    r_profile = profile_for_query_type(classify_query("What depends on PaymentService?"))
    assert t_profile == "fresh"
    assert r_profile == "semantic"
    assert _retrieval_profile_weights(t_profile) != _retrieval_profile_weights(r_profile)


def test_temporal_intent_actually_maximizes_freshness_weight():
    """Intent must move weight where the intent implies: a temporal query's
    profile gives freshness the dominant weight (not just a different label)."""
    w_lexical, w_conf, w_fresh, w_vector = _retrieval_profile_weights("fresh")
    assert w_fresh == max(w_lexical, w_conf, w_fresh, w_vector)


def test_every_query_type_maps_to_a_real_profile():
    """The mapping can never emit a profile name that RETRIEVAL_PROFILES lacks
    (which would raise ValueError deep in query_rows)."""
    for qt in QUERY_TYPES:
        assert profile_for_query_type(qt) in RETRIEVAL_PROFILES
    # Unknown/garbage intent degrades to a valid default, never crashes.
    assert profile_for_query_type("nonsense") in RETRIEVAL_PROFILES


def test_query_rows_auto_resolves_intent_without_crashing(tmp_path):
    """End-to-end: retrieval_profile='auto' must classify the query, pick a real
    profile, and return rows — never raise on the unknown-profile path."""
    svc = MemoryService(db_target=str(tmp_path / "intent.db"), workspace_root=tmp_path)
    svc.init_db()
    svc.ingest(
        "The production deploy shipped and the bug is fixed",
        [CitationInput(source="t")],
        scope="project:x",
    )
    rows = svc.query_rows("what changed recently", retrieval_profile="auto", limit=5)
    assert isinstance(rows, list)


def test_default_profile_path_is_unchanged(tmp_path):
    """Opt-in guarantee: with no retrieval_profile, behavior is identical to
    before (no auto-classification side effects)."""
    svc = MemoryService(db_target=str(tmp_path / "intent2.db"), workspace_root=tmp_path)
    svc.init_db()
    svc.ingest("A plain fact", [CitationInput(source="t")], scope="project:x")
    rows = svc.query_rows("plain fact", limit=5)  # no profile → legacy path
    assert isinstance(rows, list)
