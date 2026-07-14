"""Tests for the local cross-encoder rerank pass (evolve/local-rerank).

The real model (torch + sentence_transformers) is NEVER loaded here — the
full pytest run segfaults under torch on this box, so every scored path
uses a mocked ``score_pairs`` / a poisoned ``_get_model``. What we verify:

  * gate off (default): recall() output is byte-identical to legacy and
    the model stack is never touched;
  * gate on: the mocked scorer's ordering is applied to the rendered
    bullets and the over-fetched FTS caps kick in;
  * scoring failure: ordering falls back to the fused ranking unchanged.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from memorymaster.recall import local_rerank
from memorymaster.recall.context_hook import recall

_GATE = "MEMORYMASTER_RECALL_RERANK_LOCAL"

# Captured at import time — BEFORE the autouse fixture poisons the loader —
# so the latch test can exercise the real _get_model body.
_ORIG_GET_MODEL = local_rerank._get_model


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    """Poison the model loader so any accidental real-model path fails loudly."""
    def _boom():
        raise AssertionError("real CrossEncoder load attempted in tests")

    monkeypatch.setattr(local_rerank, "_get_model", _boom)
    monkeypatch.delenv(_GATE, raising=False)
    yield


def test_module_import_does_not_pull_torch() -> None:
    """Importing local_rerank must not import sentence_transformers/torch.

    (Guards test collection: the full suite segfaults if torch loads.)
    Only valid as written if something else didn't already import them —
    so assert conditionally on a fresh-ish interpreter state.
    """
    # local_rerank is already imported at module top; the check is that the
    # import machinery for the model stack was not triggered by it. If some
    # other test file imported torch first this assert would be vacuous,
    # hence the guard message rather than a hard requirement.
    if "sentence_transformers" in sys.modules:  # pragma: no cover
        pytest.skip("sentence_transformers imported elsewhere in this process")
    assert "torch" not in sys.modules


class TestGate:
    def test_default_off(self, monkeypatch) -> None:
        monkeypatch.delenv(_GATE, raising=False)
        assert local_rerank.local_rerank_enabled() is False

    def test_zero_off(self, monkeypatch) -> None:
        monkeypatch.setenv(_GATE, "0")
        assert local_rerank.local_rerank_enabled() is False

    def test_one_on(self, monkeypatch) -> None:
        monkeypatch.setenv(_GATE, "1")
        assert local_rerank.local_rerank_enabled() is True

    def test_overfetch_default_and_clamp(self, monkeypatch) -> None:
        monkeypatch.delenv("MEMORYMASTER_RECALL_RERANK_LOCAL_OVERFETCH", raising=False)
        assert local_rerank.overfetch_factor() == 3
        monkeypatch.setenv("MEMORYMASTER_RECALL_RERANK_LOCAL_OVERFETCH", "0")
        assert local_rerank.overfetch_factor() == 1
        monkeypatch.setenv("MEMORYMASTER_RECALL_RERANK_LOCAL_OVERFETCH", "junk")
        assert local_rerank.overfetch_factor() == 3


def _row(cid: int, text: str) -> dict:
    claim = MagicMock()
    claim.id = cid
    claim.text = text
    claim.subject = None
    claim.wiki_article = None
    claim.status = "confirmed"
    claim.scope = "project"
    claim.visibility = "public"
    claim.object_value = None
    claim.predicate = None
    return {"claim": claim, "lexical_score": 0.5, "confidence_score": 0.5}


class TestRerankRankedRows:
    def test_reorders_by_descending_score(self, monkeypatch) -> None:
        rows = [_row(1, "alpha"), _row(2, "beta"), _row(3, "gamma")]
        monkeypatch.setattr(
            local_rerank, "score_pairs", lambda q, texts: [0.1, 0.9, 0.5]
        )
        out = local_rerank.rerank_ranked_rows("q", rows)
        assert [r["claim"].id for r in out] == [2, 3, 1]

    def test_stable_on_ties(self, monkeypatch) -> None:
        rows = [_row(1, "a"), _row(2, "b"), _row(3, "c")]
        monkeypatch.setattr(local_rerank, "score_pairs", lambda q, t: [0.5, 0.5, 0.5])
        out = local_rerank.rerank_ranked_rows("q", rows)
        assert [r["claim"].id for r in out] == [1, 2, 3]

    def test_scoring_failure_returns_same_object(self, monkeypatch) -> None:
        rows = [_row(1, "a"), _row(2, "b")]
        monkeypatch.setattr(local_rerank, "score_pairs", lambda q, t: None)
        out = local_rerank.rerank_ranked_rows("q", rows)
        assert out is rows

    def test_length_mismatch_returns_same_object(self, monkeypatch) -> None:
        rows = [_row(1, "a"), _row(2, "b")]
        monkeypatch.setattr(local_rerank, "score_pairs", lambda q, t: [1.0])
        out = local_rerank.rerank_ranked_rows("q", rows)
        assert out is rows

    def test_tail_beyond_max_pairs_preserved(self, monkeypatch) -> None:
        monkeypatch.setenv("MEMORYMASTER_RECALL_RERANK_LOCAL_MAX_PAIRS", "2")
        rows = [_row(1, "a"), _row(2, "b"), _row(3, "c"), _row(4, "d")]
        monkeypatch.setattr(local_rerank, "score_pairs", lambda q, t: [0.1, 0.9])
        out = local_rerank.rerank_ranked_rows("q", rows)
        assert [r["claim"].id for r in out] == [2, 1, 3, 4]

    def test_empty_query_noop(self, monkeypatch) -> None:
        rows = [_row(1, "a"), _row(2, "b")]
        called = []
        monkeypatch.setattr(
            local_rerank, "score_pairs", lambda q, t: called.append(1) or [1.0, 0.0]
        )
        out = local_rerank.rerank_ranked_rows("   ", rows)
        assert out is rows
        assert not called

    def test_model_failure_latches_and_returns_none(self, monkeypatch) -> None:
        # Undo the autouse poison for this one test — simulate a real
        # import failure inside the ORIGINAL _get_model body instead.
        monkeypatch.setattr(local_rerank, "_model", None)
        monkeypatch.setattr(local_rerank, "_model_failed", False)
        monkeypatch.setattr(local_rerank, "_get_model", _ORIG_GET_MODEL)
        # ``import x`` with sys.modules["x"] = None raises ImportError,
        # so the loader never reaches real torch.
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)

        assert local_rerank.score_pairs("q", ["a"]) is None
        # Latch: second call short-circuits without re-importing.
        assert local_rerank._model_failed is True
        assert local_rerank.score_pairs("q", ["a"]) is None


def _mock_service_with_rows(rows: list[dict]) -> MagicMock:
    svc = MagicMock()
    svc.query_rows.return_value = rows
    return svc


class TestRecallIntegration:
    """recall() end-to-end with a mocked MemoryService (same pattern as
    tests/test_context_hook.py::TestRecall)."""

    def _rows(self) -> list[dict]:
        return [
            _row(1, "first claim about testing pipelines"),
            _row(2, "second claim about deployment gates"),
            _row(3, "third claim about rollback strategy"),
        ]

    @patch("memorymaster.core.service.MemoryService")
    def test_gate_off_byte_identical(self, mock_cls: MagicMock, monkeypatch) -> None:
        monkeypatch.delenv(_GATE, raising=False)
        mock_cls.return_value = _mock_service_with_rows(self._rows())
        baseline = recall("testing query here", db_path=":memory:", skip_qdrant=True)

        mock_cls.return_value = _mock_service_with_rows(self._rows())
        monkeypatch.setenv(_GATE, "0")
        gated_off = recall("testing query here", db_path=":memory:", skip_qdrant=True)

        assert baseline == gated_off
        assert baseline.startswith("# Memory Context")

    @patch("memorymaster.core.service.MemoryService")
    def test_gate_off_never_scores(self, mock_cls: MagicMock, monkeypatch) -> None:
        monkeypatch.delenv(_GATE, raising=False)
        mock_cls.return_value = _mock_service_with_rows(self._rows())

        def _explode(q, t):  # pragma: no cover - failure path
            raise AssertionError("score_pairs called with gate off")

        monkeypatch.setattr(local_rerank, "score_pairs", _explode)
        result = recall("testing query here", db_path=":memory:", skip_qdrant=True)
        assert result.startswith("# Memory Context")

    @patch("memorymaster.core.service.MemoryService")
    def test_gate_on_applies_mocked_ordering(
        self, mock_cls: MagicMock, monkeypatch
    ) -> None:
        monkeypatch.setenv(_GATE, "1")
        mock_cls.return_value = _mock_service_with_rows(self._rows())

        # Score inversely: last candidate wins.
        def _inverse(q: str, texts: list[str]) -> list[float]:
            return [float(i) for i in range(len(texts))]

        monkeypatch.setattr(local_rerank, "score_pairs", _inverse)
        result, ids = recall(
            "testing query here",
            db_path=":memory:",
            skip_qdrant=True,
            return_ids=True,
        )
        assert result.startswith("# Memory Context")
        # The mocked scorer gives the highest score to the LAST fused row,
        # so the bullet order must be reversed relative to gate-off.
        assert ids[0] == 3
        assert set(ids) == {1, 2, 3}

    @patch("memorymaster.core.service.MemoryService")
    def test_gate_on_overfetches_fts(self, mock_cls: MagicMock, monkeypatch) -> None:
        monkeypatch.setenv(_GATE, "1")
        svc = _mock_service_with_rows(self._rows())
        mock_cls.return_value = svc
        monkeypatch.setattr(local_rerank, "score_pairs", lambda q, t: None)

        recall("testing query here", db_path=":memory:", skip_qdrant=True)

        limits = [c.kwargs.get("limit") for c in svc.query_rows.call_args_list]
        assert limits, "query_rows never called"
        # Legacy per-token limit for this query is <= 8; over-fetch triples it.
        assert all(lim is not None and lim >= 9 for lim in limits)

    @patch("memorymaster.core.service.MemoryService")
    def test_gate_on_scoring_failure_falls_back(
        self, mock_cls: MagicMock, monkeypatch
    ) -> None:
        monkeypatch.delenv(_GATE, raising=False)
        mock_cls.return_value = _mock_service_with_rows(self._rows())
        baseline, base_ids = recall(
            "testing query here", db_path=":memory:", skip_qdrant=True, return_ids=True
        )

        monkeypatch.setenv(_GATE, "1")
        mock_cls.return_value = _mock_service_with_rows(self._rows())
        monkeypatch.setattr(local_rerank, "score_pairs", lambda q, t: None)
        degraded, deg_ids = recall(
            "testing query here", db_path=":memory:", skip_qdrant=True, return_ids=True
        )
        # Same candidates (mock returns 3 rows regardless of limit), same
        # fused ordering when the scorer fails.
        assert deg_ids == base_ids
        assert degraded == baseline
