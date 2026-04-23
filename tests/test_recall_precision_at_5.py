"""Tests for the env-var-driven recall re-ranker in ``context_hook.recall``.

Three layers:

1. ``_recall_weight`` returns shipped defaults and honors env overrides /
   invalid-input fallback.
2. ``recall`` rebuilds its formula from env weights: when we bump
   ``MEMORYMASTER_RECALL_W_FRESHNESS`` to a high value, the claim with the
   highest freshness score rises to the top.
3. Precision@5 on a synthetic 6-prompt / 20-claim fixture does not regress
   below the shipped-defaults baseline. This is the CI-safe analog of the
   30-prompt eval in ``scripts/eval_recall_precision_at_5.py`` — the live DB
   (7.3 GB) is not a CI-viable fixture.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.context_hook import (
    _RECALL_WEIGHT_DEFAULTS,
    _recall_weight,
    recall,
)
from memorymaster.models import Claim


def _claim(cid: int, text: str, *, subject: str | None = None,
           confidence: float = 0.6, wiki_article: str | None = None) -> Claim:
    return Claim(
        id=cid,
        text=text,
        idempotency_key=None,
        normalized_text=None,
        claim_type="fact",
        subject=subject,
        predicate=None,
        object_value=None,
        scope="project:test",
        volatility="medium",
        status="confirmed",
        confidence=confidence,
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-01-01",
        updated_at="2026-01-01",
        last_validated_at=None,
        archived_at=None,
        wiki_article=wiki_article,
    )


# --------------------------------------------------------------------------- #
# Layer 1 — weight loader
# --------------------------------------------------------------------------- #

def test_recall_weight_defaults_are_shipped_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, expected in _RECALL_WEIGHT_DEFAULTS.items():
        monkeypatch.delenv(f"MEMORYMASTER_RECALL_{name}", raising=False)
    for name, expected in _RECALL_WEIGHT_DEFAULTS.items():
        assert _recall_weight(name) == expected


def test_recall_weight_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_FRESHNESS", "0.42")
    assert _recall_weight("W_FRESHNESS") == 0.42


def test_recall_weight_invalid_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_VECTOR", "not-a-number")
    assert _recall_weight("W_VECTOR") == _RECALL_WEIGHT_DEFAULTS["W_VECTOR"]


def test_recall_weight_empty_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_MATCHES", "   ")
    assert _recall_weight("W_MATCHES") == _RECALL_WEIGHT_DEFAULTS["W_MATCHES"]


# --------------------------------------------------------------------------- #
# Layer 2 — env override actually changes ranking inside recall()
# --------------------------------------------------------------------------- #

class _FakeService:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def query_rows(self, **_: object) -> list[dict]:
        return list(self._rows)


def _patch_service(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> None:
    def _fake_ctor(db_target: str, workspace_root: Path):  # noqa: ARG001
        return _FakeService(rows)

    monkeypatch.setattr("memorymaster.service.MemoryService", _fake_ctor)
    # recall() also passes through extract_query_tokens; stub it so we don't
    # need a real DB on disk.
    monkeypatch.setattr(
        "memorymaster.recall_tokenizer.extract_query_tokens",
        lambda q, db, max_tokens=6: "steward",
    )


def test_env_boost_to_freshness_promotes_fresh_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """With W_FRESHNESS high, the claim with freshness_score=1.0 leads."""
    lexical_winner = _claim(1, "steward training ran with ok recall")
    freshness_winner = _claim(2, "steward tuning finished cleanly")
    rows = [
        {"claim": lexical_winner, "lexical_score": 0.95, "freshness_score": 0.0,
         "confidence_score": 0.5, "vector_score": 0.0},
        {"claim": freshness_winner, "lexical_score": 0.10, "freshness_score": 1.0,
         "confidence_score": 0.5, "vector_score": 0.0},
    ]
    _patch_service(monkeypatch, rows)

    # Default weights: lexical_winner wins (matches + lexical dominate).
    monkeypatch.delenv("MEMORYMASTER_RECALL_W_FRESHNESS", raising=False)
    default_out = recall("steward", db_path=str(tmp_path / "nope.db"), skip_qdrant=True)
    assert "steward training" in default_out
    assert default_out.index("steward training") < default_out.index("steward tuning")

    # Boost freshness: the freshness_winner should now lead.
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_FRESHNESS", "10.0")
    boosted_out = recall("steward", db_path=str(tmp_path / "nope.db"), skip_qdrant=True)
    assert boosted_out.index("steward tuning") < boosted_out.index("steward training")


def test_env_zeroing_all_weights_is_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Zeroing every weight still produces output without crashing."""
    for name in _RECALL_WEIGHT_DEFAULTS:
        monkeypatch.setenv(f"MEMORYMASTER_RECALL_{name}", "0.0")
    rows = [
        {"claim": _claim(1, "alpha"), "lexical_score": 0.5, "freshness_score": 0.5,
         "confidence_score": 0.5, "vector_score": 0.5},
        {"claim": _claim(2, "beta"), "lexical_score": 0.5, "freshness_score": 0.5,
         "confidence_score": 0.5, "vector_score": 0.5},
    ]
    _patch_service(monkeypatch, rows)
    out = recall("alpha", db_path=str(tmp_path / "nope.db"), skip_qdrant=True)
    assert "Memory Context" in out


# --------------------------------------------------------------------------- #
# Layer 3 — precision@5 floor on a synthetic fixture
# --------------------------------------------------------------------------- #

# 6 prompts, each with an intentionally relevant claim + 4 distractors.
# The synthetic setup guarantees the top-ranked candidate *can* be the
# relevant one under default weights — so this pins regressions, not
# absolute quality.
_PROMPTS_AND_RELEVANT = [
    ("steward classifier calibration", "steward classifier calibration run shipped"),
    ("qdrant vector search deployment", "qdrant vector search deployment is stable"),
    ("wiki article absorb flow", "wiki article absorb flow refactored yesterday"),
    ("entity extraction at ingest", "entity extraction at ingest now regex based"),
    ("recall hook tokenizer stopwords", "recall hook tokenizer stopwords in spanish"),
    ("mcp server auto citation", "mcp server auto citation fallback for new tools"),
]

_DISTRACTORS = [
    "banana bread recipe version two",
    "calendar offsets for argentina timezone",
    "deploy demo page with cloudflared tunnel",
    "pull request enhancement checklist",
    "obsidian canvas file format quirks",
    "schema migrations for sqlite wal mode",
]


def _build_fixture_rows(relevant_text: str, *, lexical: float = 0.9,
                        freshness: float = 0.5, confidence: float = 0.7) -> list[dict]:
    relevant = _claim(1, relevant_text)
    rows = [{"claim": relevant, "lexical_score": lexical,
             "freshness_score": freshness, "confidence_score": confidence,
             "vector_score": 0.0}]
    for i, text in enumerate(_DISTRACTORS, start=2):
        rows.append({
            "claim": _claim(i, text),
            # Distractors get noisy but lower lexical relevance.
            "lexical_score": 0.2 + (i * 0.05) % 0.3,
            "freshness_score": 0.4,
            "confidence_score": 0.5,
            "vector_score": 0.0,
        })
    return rows


def _rank_top5_contains_relevant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    prompt: str, relevant_text: str,
) -> bool:
    rows = _build_fixture_rows(relevant_text)
    _patch_service(monkeypatch, rows)
    # extract_query_tokens returns the prompt unchanged (stub).
    monkeypatch.setattr(
        "memorymaster.recall_tokenizer.extract_query_tokens",
        lambda q, db, max_tokens=6: q,
    )
    out = recall(prompt, db_path=str(tmp_path / "nope.db"), skip_qdrant=True)
    # Find positions of all claims in output; the relevant one must be in top 5.
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    for idx, line in enumerate(lines[:5]):
        if relevant_text in line:
            return True
    return False


def test_precision_at_5_floor_with_default_weights(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """On the synthetic fixture every prompt must surface its relevant claim
    in the top-5 with the shipped default weights. This is the CI floor —
    regressions that demote lexical-matching claims out of top-5 will trip it.

    The real 30-prompt p@5 baseline on the live DB is ~0.66 (top_k=8,
    min_overlap=1); see ``artifacts/eval/recall-precision-grid-k8-mov1.jsonl``.
    """
    for name in _RECALL_WEIGHT_DEFAULTS:
        monkeypatch.delenv(f"MEMORYMASTER_RECALL_{name}", raising=False)

    hits = 0
    for prompt, relevant in _PROMPTS_AND_RELEVANT:
        if _rank_top5_contains_relevant(monkeypatch, tmp_path, prompt, relevant):
            hits += 1
    # 6/6 must surface — this is a degenerate floor. The live 30-prompt
    # eval yields ~0.66 because of candidate-pool gaps, not ranking errors.
    assert hits == len(_PROMPTS_AND_RELEVANT), (
        f"synthetic p@5 floor broken: {hits}/{len(_PROMPTS_AND_RELEVANT)}"
    )


def test_precision_at_5_floor_with_grid_winner_weights(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The autoresearch grid winner weights (freshness boosted) must not
    regress the synthetic p@5 floor either."""
    overrides = {
        "W_MATCHES": "0.5", "W_PHRASE": "0.0", "W_ALL": "0.0",
        "W_LEXICAL": "0.0", "W_CONFIDENCE": "0.5",
        "W_FRESHNESS": "0.15", "W_VECTOR": "0.0",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(f"MEMORYMASTER_RECALL_{name}", value)

    hits = 0
    for prompt, relevant in _PROMPTS_AND_RELEVANT:
        if _rank_top5_contains_relevant(monkeypatch, tmp_path, prompt, relevant):
            hits += 1
    assert hits >= len(_PROMPTS_AND_RELEVANT) - 1, (
        f"grid-winner p@5 floor broken: {hits}/{len(_PROMPTS_AND_RELEVANT)}"
    )
