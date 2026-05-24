"""Floor-ratio gate on metadata boosts (v3.22, ported from gbrain v0.35.6).

The gate suppresses non-relevance boosts (confidence/freshness/pinned/tier) on
candidates whose query-relevance (lexical+vector) is below
``boost_floor_ratio * top_relevance``. This stops a weak-but-fresh/confident
claim from outranking the true lexical match — a real failure mode with a
strong embedder + metadata boosting.

Weights are pinned via env so the scenario is deterministic regardless of the
shipped default blend.
"""
from __future__ import annotations

import pytest

from memorymaster.config import reset_config
from memorymaster.models import Claim
from memorymaster.retrieval import rank_claim_rows, rank_claims

_OLD = "2000-01-01T00:00:00+00:00"      # stale -> freshness ~0
_FUTURE = "2999-01-01T00:00:00+00:00"   # fresh -> freshness ~1
_QUERY = "postgresql database version"


def _claim(cid: int, text: str, *, confidence: float, pinned: bool, date: str) -> Claim:
    return Claim(
        id=cid, text=text, idempotency_key=None, normalized_text=None,
        claim_type=None, subject=None, predicate=None, object_value=None,
        scope="project", volatility="medium", status="confirmed",
        confidence=confidence, pinned=pinned, supersedes_claim_id=None,
        replaced_by_claim_id=None, created_at=date, updated_at=date,
        last_validated_at=None, archived_at=None,
    )


def _claims() -> list[Claim]:
    # True match: matches all 3 query tokens, but stale + zero confidence.
    true_match = _claim(
        1, "the production server uses postgresql database version sixteen",
        confidence=0.0, pinned=False, date=_OLD,
    )
    # Decoy: matches 1 of 3 query tokens, but max confidence + fresh + pinned.
    decoy = _claim(
        2, "postgresql tutorial introduction beginners guide notes",
        confidence=1.0, pinned=True, date=_FUTURE,
    )
    return [true_match, decoy]


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    # Confidence-heavy no-vector blend so boosts can dominate when ungated.
    monkeypatch.setenv("MEMORYMASTER_RETRIEVAL_WEIGHTS_NO_VECTOR", "0.3,0.6,0.1")
    monkeypatch.delenv("MEMORYMASTER_BOOST_FLOOR_RATIO", raising=False)
    reset_config()
    yield
    reset_config()


def test_without_gate_fresh_decoy_outranks_true_match(monkeypatch):
    """Baseline: with the gate off (default), the fresh/confident decoy wins
    even though the true match is the better lexical hit."""
    reset_config()
    ranked = rank_claims(_QUERY, _claims(), mode="hybrid", limit=2)
    assert ranked[0].id == 2  # decoy outranks the true match — the bug we fix


def test_gate_demotes_weak_match_so_true_match_wins(monkeypatch):
    """With the floor gate on, the decoy's boosts are suppressed (its relevance
    is below the floor), so the true lexical match ranks first."""
    monkeypatch.setenv("MEMORYMASTER_BOOST_FLOOR_RATIO", "0.6")
    reset_config()
    ranked = rank_claims(_QUERY, _claims(), mode="hybrid", limit=2)
    assert ranked[0].id == 1  # true match now wins


def test_gate_breakdown_flags_suppressed_boosts(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BOOST_FLOOR_RATIO", "0.6")
    reset_config()
    rows = {r.claim.id: r for r in rank_claim_rows(_QUERY, _claims(), mode="hybrid", limit=2)}
    assert rows[1].breakdown["boosts_applied"] is True   # true match keeps boosts
    assert rows[2].breakdown["boosts_applied"] is False  # decoy boosts gated off
    # The decoy's gated score equals its bare relevance (no boosts added).
    assert rows[2].breakdown["final"] == pytest.approx(rows[2].breakdown["relevance"])


def test_gate_disabled_by_default_applies_all_boosts(monkeypatch):
    reset_config()
    rows = rank_claim_rows(_QUERY, _claims(), mode="hybrid", limit=2)
    assert all(r.breakdown["boosts_applied"] for r in rows)  # floor_ratio=0 -> never gated
