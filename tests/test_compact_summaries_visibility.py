"""T-1865 / F10: a compact summary is never more visible than its sources.

Archived private claims used to be summarised into a PUBLIC, auto-confirmed claim
(``create_claim`` was called without visibility, so it defaulted to public).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.govern.jobs.compact_summaries import run
from memorymaster.stores.storage import SQLiteStore

LLM_REPLY = json.dumps({
    "summary_text": "Consolidated summary of the archived cluster.",
    "subject": "topic", "predicate": "summary_of", "object_value": "conclusion", "confidence": 0.9,
})


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "test.db")
    s.init_db()
    return s


def _archived(store, text, *, visibility="public", source_agent=None, scope="project:p"):
    claim = store.create_claim(
        text=text, citations=[CitationInput(source="test")], subject="topic",
        predicate="has_value", object_value="v", scope=scope,
        visibility=visibility, source_agent=source_agent,
    )
    actor = {"actor": "operator"}
    transition_claim(store, claim.id, to_status="confirmed", reason="t", event_type="transition", event_payload=actor)
    transition_claim(store, claim.id, to_status="stale", reason="t", event_type="decay")
    transition_claim(store, claim.id, to_status="archived", reason="t", event_type="compactor")


def _summaries(store):
    return [c for c in store.list_claims(limit=100, include_archived=True) if c.claim_type == "summary"]


def _run(store):
    with patch("memorymaster.govern.jobs.compact_summaries._call_llm", return_value=LLM_REPLY):
        return run(store, provider="custom", api_key="x", min_cluster=3)


def test_private_sources_yield_private_unconfirmed_summary(store):
    for i in range(3):
        _archived(store, f"private fact {i}", visibility="private", source_agent="alice")
    _run(store)
    [summary] = _summaries(store)
    assert (summary.visibility, summary.source_agent) == ("private", "alice")
    assert summary.status == "candidate"  # no auto-confirm across private sources


def test_sensitive_sources_yield_sensitive_summary(store):
    for i in range(3):
        _archived(store, f"sensitive fact {i}", visibility="sensitive", source_agent="alice")
    _run(store)
    [summary] = _summaries(store)
    assert summary.visibility == "sensitive" and summary.status == "candidate"


def test_clusters_never_mix_owners_or_visibilities(store):
    for i in range(3):
        _archived(store, f"alice fact {i}", visibility="private", source_agent="alice")
        _archived(store, f"bob fact {i}", visibility="private", source_agent="bob")
        _archived(store, f"public fact {i}")
    _run(store)
    seen = sorted((s.visibility, s.source_agent) for s in _summaries(store))
    assert seen == [("private", "alice"), ("private", "bob"), ("public", None)]
    public = next(s for s in _summaries(store) if s.visibility == "public")
    assert public.status == "confirmed"  # public-only clusters keep the old behaviour


def test_two_private_claims_do_not_borrow_a_third_owners_cluster(store):
    _archived(store, "alice a", visibility="private", source_agent="alice")
    _archived(store, "alice b", visibility="private", source_agent="alice")
    _archived(store, "bob a", visibility="private", source_agent="bob")
    result = _run(store)
    assert result.summaries_created == 0  # no owner reaches min_cluster alone
    assert _summaries(store) == []
