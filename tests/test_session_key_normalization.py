"""F-11: explicit session keys are normalised and hashed with the tenant only.

Review repro ``review-C/r5_recall_key.py``: one session written six ways (case,
trailing whitespace, ``\\`` vs ``/``, bare id vs transcript path) counted as six
sessions, so the diversity cap (3) kept 6/6; the same session under two
``source_agent`` values also escaped the cap.  The key now normalises the
locator (strip, path stem, casefold) and hashes ``(tenant, session)`` only.
"""
from __future__ import annotations

import hashlib

import pytest

from memorymaster.core.config import reset_config
from memorymaster.core.models import Citation, Claim
from memorymaster.recall import drop_trace
from memorymaster.recall.retrieval import (
    RankedClaim,
    _source_session_key,
    apply_session_diversity_cap,
)


@pytest.fixture(autouse=True)
def _reset_config():
    reset_config()
    yield
    reset_config()


def _row(claim_id: int, locator: str | None, *, agent: str = "claude-session",
         tenant: str | None = None) -> RankedClaim:
    claim = Claim(
        id=claim_id, text="alpha", idempotency_key=None, normalized_text=None,
        claim_type=None, subject=None, predicate=None, object_value=None,
        scope="project", volatility="medium", status="confirmed", confidence=0.8,
        pinned=False, supersedes_claim_id=None, replaced_by_claim_id=None,
        created_at="2999-01-01T00:00:00+00:00", updated_at="2999-01-01T00:00:00+00:00",
        last_validated_at=None, archived_at=None, source_agent=agent, tenant_id=tenant,
    )
    if locator is not None:
        claim.citations = [Citation(1, claim_id, "session", locator, None, "")]
    return RankedClaim(claim=claim, score=1 / claim_id, lexical_score=0.0,
                       vector_score=0.0, confidence_score=0.0, freshness_score=0.0)


def _kept(rows, cap=3):
    return [row.claim.id for row in apply_session_diversity_cap(rows, cap)]


def test_one_session_written_six_ways_is_one_session():
    variants = [
        "abc-123",
        "ABC-123",
        "abc-123 ",
        r"C:\Users\x\.claude\projects\p\abc-123.jsonl",
        "C:/Users/x/.claude/projects/p/abc-123.jsonl",
        "c:/users/x/.claude/projects/p/abc-123.jsonl",
    ]
    rows = [_row(i, loc) for i, loc in enumerate(variants, start=1)]
    assert _kept(rows) == [1, 2, 3]


def test_same_session_under_different_source_agents_is_one_session():
    rows = [_row(i, "S1", agent=["claude-session", "dream-worker"][i % 2]) for i in range(1, 7)]
    assert _kept(rows) == [1, 2, 3]


def test_case_variants_on_one_claim_are_not_ambiguous():
    row = _row(1, "Session-A")
    row.claim.citations.append(Citation(2, 1, "session", " session-a ", None, ""))
    assert _source_session_key(row).startswith("session:")


def test_tenant_remains_part_of_the_key():
    rows = [_row(i, "S1", tenant="ab"[i % 2]) for i in range(1, 9)]
    assert len(_kept(rows)) == 6


def test_key_hashes_tenant_and_normalized_session_only():
    row = _row(1, r"C:\Users\Secret\Projects\Abc-123.JSONL ", tenant="tenant-a")
    expected = "session:" + hashlib.sha256(repr(("tenant-a", "abc-123")).encode("utf-8")).hexdigest()
    assert _source_session_key(row) == expected


def test_distinct_sessions_survive_and_raw_locator_never_reaches_drop_trace():
    distinct = [_row(i, f"session-{i}") for i in range(1, 6)]
    assert _kept(distinct) == [1, 2, 3, 4, 5]
    with drop_trace.recording() as drops:
        apply_session_diversity_cap([_row(i, r"C:\Users\secret\path.jsonl") for i in range(1, 6)], 3)
    assert drops, "the fourth and fifth rows must be dropped"
    assert not any("secret" in str(drop).lower() for drop in drops)


def test_dotted_session_ids_are_not_truncated():
    """Verifier note: ``run.1`` and ``run.2`` both became ``run`` and over-capped.
    Only a transcript file extension is dropped, not any dotted segment."""
    rows = [_row(i, f"run.{i}") for i in range(1, 6)]
    assert _kept(rows) == [1, 2, 3, 4, 5]
    same = [_row(1, "RUN.7"), _row(2, "/logs/run.7.jsonl"), _row(3, "run.7.LOG"), _row(4, "run.7")]
    assert _kept(same, cap=1) == [1]
