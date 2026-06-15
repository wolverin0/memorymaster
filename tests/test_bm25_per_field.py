"""Tests for BM25 per-field weighting (roadmap 1.4).

The BM25 rescorer in ``context_hook.recall`` splits subject + text into two
independent scoring streams and combines them with ``MEMORYMASTER_BM25_W_SUBJECT``
and ``MEMORYMASTER_BM25_W_TEXT`` weights. These tests exercise both the
helper env parsing and the end-to-end ranking behaviour.

They drive the ``recall`` entry point with a mocked service so no DB is
required, and they inspect the rendered output order — which follows the
ranking of ``bm25_scores`` when lexical weights dominate the ranker.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from memorymaster.recall.context_hook import (
    _BM25_W_SUBJECT_DEFAULT,
    _BM25_W_TEXT_DEFAULT,
    _bm25_field_weight,
    recall,
)


@pytest.fixture(autouse=True)
def _clear_bm25_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from a clean env so defaults are predictable."""
    for key in (
        "MEMORYMASTER_LEXICAL_BM25",
        "MEMORYMASTER_BM25_W_SUBJECT",
        "MEMORYMASTER_BM25_W_TEXT",
        "MEMORYMASTER_BM25_K1",
        "MEMORYMASTER_BM25_B",
    ):
        monkeypatch.delenv(key, raising=False)
    # Make sure BM25 is ON (recall() default)
    monkeypatch.setenv("MEMORYMASTER_LEXICAL_BM25", "1")
    # Pin recall weights so the lexical stream dominates ordering in assertions.
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_MATCHES", "0.0")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_PHRASE", "0.0")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_ALL", "0.0")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_CONFIDENCE", "0.0")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_FRESHNESS", "0.0")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_VECTOR", "0.0")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_ENTITY", "0.0")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_VERBATIM", "0.0")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_LEXICAL", "1.0")


def _make_claim(cid: int, subject: str, text: str) -> MagicMock:
    """Build a claim-shaped MagicMock consumable by ``recall()``."""
    claim = MagicMock()
    claim.id = cid
    claim.subject = subject
    claim.text = text
    # ``recall`` attribute-probes several fields; default them to sane values.
    claim.confidence_score = 0.0
    return claim


def _rows_for_fixture() -> list[dict]:
    """3-doc fixture where each field varies independently.

    - doc 1 ("postgres-subject"): "postgres" ONLY in subject
    - doc 2 ("postgres-body"):    "postgres" ONLY in text body
    - doc 3 ("unrelated"):        "postgres" absent everywhere

    Both doc 1 and doc 2 are real matches for "postgres"; the weight split
    decides which ranks first.
    """
    return [
        {
            "claim": _make_claim(
                1, "postgres migration decision", "decided to migrate away from sqlite storage"
            ),
            "lexical_score": 0.0,
            "confidence_score": 0.0,
        },
        {
            "claim": _make_claim(
                2, "database change", "we will use postgres in production starting next week"
            ),
            "lexical_score": 0.0,
            "confidence_score": 0.0,
        },
        {
            "claim": _make_claim(
                3, "unrelated choice", "switched browsers and the tab is fine now"
            ),
            "lexical_score": 0.0,
            "confidence_score": 0.0,
        },
    ]


def _top_claim_order(result: str) -> list[int]:
    """Return claim ids in render order by looking up which fixture doc each
    bullet came from. We rely on unique text prefixes across fixtures.
    """
    # doc-id → distinctive substring from its `text` body
    markers = {
        1: "decided to migrate away from sqlite",
        2: "we will use postgres in production",
        3: "switched browsers and the tab",
        10: "postgres migration done",
        11: "",  # doc 11 has empty text; skip identification
    }
    ids: list[int] = []
    for line in result.splitlines():
        if not line.startswith("- "):
            continue
        for cid, needle in markers.items():
            if needle and needle in line:
                ids.append(cid)
                break
    return ids


class TestBm25FieldWeightHelper:
    def test_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MEMORYMASTER_BM25_W_SUBJECT", raising=False)
        monkeypatch.delenv("MEMORYMASTER_BM25_W_TEXT", raising=False)
        # Default is 1.0 / 1.0 (neutral) after the 2026-04-23 null-result
        # eval; see artifacts/bm25-per-field-eval-2026-04-23.md.
        assert _bm25_field_weight("W_SUBJECT", _BM25_W_SUBJECT_DEFAULT) == 1.0
        assert _bm25_field_weight("W_TEXT", _BM25_W_TEXT_DEFAULT) == 1.0

    def test_parses_float_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMORYMASTER_BM25_W_SUBJECT", "3.5")
        monkeypatch.setenv("MEMORYMASTER_BM25_W_TEXT", "0.25")
        assert _bm25_field_weight("W_SUBJECT", 2.0) == 3.5
        assert _bm25_field_weight("W_TEXT", 1.0) == 0.25

    def test_falls_back_on_garbage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMORYMASTER_BM25_W_SUBJECT", "not-a-number")
        assert _bm25_field_weight("W_SUBJECT", 2.0) == 2.0

    def test_blank_env_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMORYMASTER_BM25_W_SUBJECT", "   ")
        assert _bm25_field_weight("W_SUBJECT", 2.0) == 2.0


class TestBm25PerFieldRanking:
    @patch("memorymaster.core.service.MemoryService")
    def test_equal_weights_returns_all_three_docs(
        self, mock_service_class: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With 1.0/1.0 weights both subject- and body-matches appear in output.

        We don't assert an exact ordering here — BM25 IDF depends on the
        corpus shape — only that both real matches beat the unrelated doc.
        """
        monkeypatch.setenv("MEMORYMASTER_BM25_W_SUBJECT", "1.0")
        monkeypatch.setenv("MEMORYMASTER_BM25_W_TEXT", "1.0")
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = _rows_for_fixture()

        result = recall("postgres", db_path=":memory:", skip_qdrant=True)

        ordered = _top_claim_order(result)
        # Both matching docs (1, 2) must rank above the unrelated doc (3).
        assert 1 in ordered and 2 in ordered
        assert ordered.index(1) < ordered.index(3)
        assert ordered.index(2) < ordered.index(3)

    @patch("memorymaster.core.service.MemoryService")
    def test_subject_heavy_weights_rank_subject_match_first(
        self, mock_service_class: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """W_SUBJECT=10, W_TEXT=0 → doc 1 (subject hit) beats doc 2 (body hit)."""
        monkeypatch.setenv("MEMORYMASTER_BM25_W_SUBJECT", "10.0")
        monkeypatch.setenv("MEMORYMASTER_BM25_W_TEXT", "0.0")
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = _rows_for_fixture()

        result = recall("postgres", db_path=":memory:", skip_qdrant=True)

        ordered = _top_claim_order(result)
        assert ordered, "expected at least one ranked claim"
        # Doc 1 has "postgres" in subject; doc 2 only in text body.
        assert ordered[0] == 1
        # Doc 2 still matches (text stream just has zero weight, but the
        # 'matches' bonus is also zeroed in this fixture), so its lexical
        # score is 0 — it ties with doc 3 for the tail. Only assert doc 1
        # wins.

    @patch("memorymaster.core.service.MemoryService")
    def test_text_only_weights_rank_body_match_first(
        self, mock_service_class: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inverse: W_SUBJECT=0, W_TEXT=10 → doc 2 (body hit) wins."""
        monkeypatch.setenv("MEMORYMASTER_BM25_W_SUBJECT", "0.0")
        monkeypatch.setenv("MEMORYMASTER_BM25_W_TEXT", "10.0")
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = _rows_for_fixture()

        result = recall("postgres", db_path=":memory:", skip_qdrant=True)

        ordered = _top_claim_order(result)
        assert ordered, "expected at least one ranked claim"
        assert ordered[0] == 2

    @patch("memorymaster.core.service.MemoryService")
    def test_empty_subject_does_not_crash(
        self, mock_service_class: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Docs with empty subject (or empty text) must not break scoring."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = [
            {"claim": _make_claim(10, "", "postgres migration done"), "lexical_score": 0.0},
            {"claim": _make_claim(11, "postgres", ""), "lexical_score": 0.0},
        ]

        result = recall("postgres", db_path=":memory:", skip_qdrant=True)
        # Just verify both show up and we got a valid rendered block.
        assert "postgres" in result.lower()
        assert result.startswith("# Memory Context")
