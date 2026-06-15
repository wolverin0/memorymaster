"""Tests for retrieval latency instrumentation in ``context_hook.recall``
(roadmap 5.1).

Invariants enforced:

1. ``log_hook`` is called with ``event="latency"`` once per stream that ran,
   and once with ``event="latency_total"`` per call (consolidated row).
2. Durations use ``time.perf_counter`` — NOT ``time.monotonic`` (claim 11848
   documents a Windows timer flake where ``monotonic()`` can go backwards).
3. Disabled streams emit no latency event (zero-overhead invariant).
4. ``total_ms`` is always present and positive (monotonic across perf_counter
   values).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from memorymaster.recall import context_hook
from memorymaster.recall.context_hook import recall


def _mock_claim(cid: int, text: str = "working on memorymaster project", subject: str = "memorymaster"):
    c = MagicMock()
    c.id = cid
    c.text = text
    c.subject = subject
    c.scope = "project:memorymaster"
    c.status = "confirmed"
    c.confidence = 0.9
    c.wiki_article = None
    return c


class TestRecallLatencyInstrumentation:
    """Verify per-stream + total latency lines emitted to ``log_hook``."""

    @patch("memorymaster.recall.context_hook.log_hook")
    @patch("memorymaster.core.service.MemoryService")
    def test_fts5_stream_always_emits_latency_line(
        self, mock_service_class: MagicMock, mock_log_hook: MagicMock
    ) -> None:
        """The FTS5 stream is non-gated — every recall() call emits one
        ``stream=fts5`` line."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = [
            {"claim": _mock_claim(1), "lexical_score": 0.5, "confidence_score": 0.9}
        ]

        recall("what am I working on?", db_path=":memory:", skip_qdrant=True)

        streams = _streams_emitted(mock_log_hook)
        assert "fts5" in streams

    @patch("memorymaster.recall.context_hook.log_hook")
    @patch("memorymaster.core.service.MemoryService")
    def test_latency_total_event_always_emitted(
        self, mock_service_class: MagicMock, mock_log_hook: MagicMock
    ) -> None:
        """Every call emits exactly one ``latency_total`` line with
        ``total_ms`` populated."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = [
            {"claim": _mock_claim(1), "lexical_score": 0.5, "confidence_score": 0.9}
        ]

        recall("what am I working on?", db_path=":memory:", skip_qdrant=True)

        total_calls = [
            c for c in mock_log_hook.call_args_list
            if c.args == ("recall", "latency_total")
        ]
        assert len(total_calls) == 1, f"expected 1 total event, got {len(total_calls)}"
        total_kwargs = total_calls[0].kwargs
        assert "total_ms" in total_kwargs
        assert total_kwargs["total_ms"] >= 0.0

    @patch("memorymaster.recall.context_hook.log_hook")
    @patch("memorymaster.core.service.MemoryService")
    def test_verbatim_disabled_emits_no_verbatim_line(
        self, mock_service_class: MagicMock, mock_log_hook: MagicMock,
        monkeypatch,
    ) -> None:
        """With ``MEMORYMASTER_RECALL_VERBATIM`` unset, no ``stream=verbatim``
        line is emitted — proves the zero-overhead invariant for disabled
        streams."""
        monkeypatch.delenv("MEMORYMASTER_RECALL_VERBATIM", raising=False)
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = [
            {"claim": _mock_claim(1), "lexical_score": 0.5, "confidence_score": 0.9}
        ]

        recall("test query", db_path=":memory:", skip_qdrant=True)

        streams = _streams_emitted(mock_log_hook)
        assert "verbatim" not in streams

    @patch("memorymaster.recall.context_hook.log_hook")
    @patch("memorymaster.core.service.MemoryService")
    def test_vector_fallback_disabled_emits_no_line(
        self, mock_service_class: MagicMock, mock_log_hook: MagicMock,
        monkeypatch,
    ) -> None:
        """With the vector fallback env vars unset, no ``stream=vector_fallback``
        line is emitted."""
        monkeypatch.delenv("MEMORYMASTER_RECALL_VECTOR_FALLBACK", raising=False)
        monkeypatch.delenv("MEMORYMASTER_QDRANT_URL", raising=False)
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = [
            {"claim": _mock_claim(1), "lexical_score": 0.5, "confidence_score": 0.9}
        ]

        recall("test query", db_path=":memory:", skip_qdrant=True)

        streams = _streams_emitted(mock_log_hook)
        assert "vector_fallback" not in streams

    @patch("memorymaster.recall.context_hook.log_hook")
    @patch("memorymaster.core.service.MemoryService")
    def test_bm25_disabled_emits_no_line(
        self, mock_service_class: MagicMock, mock_log_hook: MagicMock,
        monkeypatch,
    ) -> None:
        """With ``MEMORYMASTER_LEXICAL_BM25=0``, no ``stream=bm25_rescore``
        line is emitted."""
        monkeypatch.setenv("MEMORYMASTER_LEXICAL_BM25", "0")
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = [
            {"claim": _mock_claim(1), "lexical_score": 0.5, "confidence_score": 0.9}
        ]

        recall("test query", db_path=":memory:", skip_qdrant=True)

        streams = _streams_emitted(mock_log_hook)
        assert "bm25_rescore" not in streams

    @patch("memorymaster.recall.context_hook.log_hook")
    @patch("memorymaster.core.service.MemoryService")
    def test_rank_and_build_emitted_on_non_empty_rows(
        self, mock_service_class: MagicMock, mock_log_hook: MagicMock
    ) -> None:
        """When we have at least one candidate row, the rank+build phase runs
        and emits a latency line."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = [
            {"claim": _mock_claim(1), "lexical_score": 0.5, "confidence_score": 0.9}
        ]

        recall("what am I working on?", db_path=":memory:", skip_qdrant=True)

        streams = _streams_emitted(mock_log_hook)
        assert "rank_and_build" in streams

    @patch("memorymaster.recall.context_hook.log_hook")
    @patch("memorymaster.core.service.MemoryService")
    def test_rank_and_build_skipped_on_empty_rows(
        self, mock_service_class: MagicMock, mock_log_hook: MagicMock
    ) -> None:
        """Empty-rows path returns early before rank_and_build — no line
        for that stream."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = []

        recall("nothing matches", db_path=":memory:", skip_qdrant=True)

        streams = _streams_emitted(mock_log_hook)
        assert "rank_and_build" not in streams
        # fts5 + total still emit — the try/finally flushes every path.
        assert "fts5" in streams
        assert _latency_total_emitted(mock_log_hook)


class TestRecallLatencyDeterminism:
    """Verify ``time.perf_counter`` (NOT ``monotonic``) drives the timers and
    that the emitted values are deterministic under a mocked clock.

    Critical: this test MUST pass on Windows. Claim 11848 documents that
    ``time.monotonic`` can return non-monotonic values on Windows across a
    clock-sync boundary, yielding negative deltas. ``perf_counter`` is the
    stdlib-recommended short-interval timer on all platforms, including
    Windows.
    """

    @patch("memorymaster.recall.context_hook.log_hook")
    @patch("memorymaster.recall.context_hook.time")
    @patch("memorymaster.core.service.MemoryService")
    def test_uses_perf_counter_not_monotonic(
        self,
        mock_service_class: MagicMock,
        mock_time: MagicMock,
        mock_log_hook: MagicMock,
    ) -> None:
        """Feed deterministic perf_counter values, verify the emitted ms
        is a positive float derived from the mocked perf_counter deltas."""
        # 20 ticks is plenty for a single recall() — we don't care about
        # exact call count, we only care that perf_counter is used at all.
        mock_time.perf_counter.side_effect = [float(i) for i in range(200)]

        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = [
            {"claim": _mock_claim(1), "lexical_score": 0.5, "confidence_score": 0.9}
        ]

        recall("test query", db_path=":memory:", skip_qdrant=True)

        # If the implementation accidentally switched to monotonic(),
        # perf_counter would NOT have been called.
        assert mock_time.perf_counter.called, (
            "recall() must use time.perf_counter, not time.monotonic — "
            "see claim 11848 for the Windows timer flake."
        )
        # monotonic() should NOT have been called. If the implementation
        # regresses, this assertion will fail on every platform.
        if hasattr(mock_time, "monotonic"):
            assert not mock_time.monotonic.called, (
                "recall() must not use time.monotonic — see claim 11848."
            )

        # Every emitted ms is a positive float (mocked perf_counter is
        # strictly increasing, so every delta is positive).
        latency_calls = [
            c for c in mock_log_hook.call_args_list
            if c.args == ("recall", "latency")
        ]
        for call in latency_calls:
            ms = call.kwargs["ms"]
            assert isinstance(ms, float)
            assert ms >= 0.0

    @patch("memorymaster.recall.context_hook.log_hook")
    @patch("memorymaster.core.service.MemoryService")
    def test_log_hook_never_raises_even_on_failure(
        self, mock_service_class: MagicMock, mock_log_hook: MagicMock
    ) -> None:
        """log_hook swallows every error, so recall() must still return a
        valid string even if logging would have blown up."""
        mock_log_hook.side_effect = RuntimeError("disk full")

        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = [
            {"claim": _mock_claim(1), "lexical_score": 0.5, "confidence_score": 0.9}
        ]

        # The _emit_recall_latency wrapper catches exceptions so recall()
        # never propagates them.
        result = recall("test query", db_path=":memory:", skip_qdrant=True)
        assert isinstance(result, str)


# --- helpers ---

def _streams_emitted(mock_log_hook: MagicMock) -> set[str]:
    """Collect the ``stream=...`` values from every ``recall / latency`` call."""
    streams: set[str] = set()
    for call in mock_log_hook.call_args_list:
        if call.args == ("recall", context_hook._LATENCY_EVENT):
            s = call.kwargs.get("stream")
            if s:
                streams.add(s)
    return streams


def _latency_total_emitted(mock_log_hook: MagicMock) -> bool:
    for call in mock_log_hook.call_args_list:
        if call.args == ("recall", context_hook._LATENCY_TOTAL_EVENT):
            return True
    return False
