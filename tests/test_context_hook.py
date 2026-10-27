"""Tests for context_hook — automatic memory extraction and injection.

Tests cover:
  - classify_observation: pattern matching for observation types
  - recall: mocked query against memory database
  - observe: mocked ingestion of observations
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from memorymaster.recall.context_hook import classify_observation, observe, recall


def test_governed_prompt_rows_keep_exact_then_fill_without_recording_discarded(monkeypatch):
    from memorymaster.recall.context_hook import _governed_prompt_rows

    monkeypatch.setenv("MEMORYMASTER_SCOPE_DEFAULT", "project:fixture")
    query = "deployment worker retry queue timeout"
    rows = [
        {"claim": SimpleNamespace(
            id=n, text=f"deployment worker note {n}", status="confirmed",
            scope="project:fixture", visibility="public", subject=None,
            predicate=None, object_value=None, valid_from=None, valid_until=None,
        )}
        for n in range(1, 5)
    ]
    service = MagicMock()
    # Duplicate exact match must not consume a slot; the last row is discarded.
    service.query_rows.side_effect = [rows[:1], [rows[1], rows[0], *rows[2:]]]

    result = _governed_prompt_rows(service, query, limit=3)

    assert [row["claim"].id for row in result] == [1, 2, 3]
    calls = [call.kwargs for call in service.query_rows.call_args_list]
    assert [call["query_text"] for call in calls] == [query, " OR ".join(query.split())]
    for call in calls:
        assert call["limit"] == 3
        assert call["record_accesses"] is False
        assert call["include_stale"] is False
        assert call["include_conflicted"] is False
        assert call["include_candidates"] is False
        assert "project:fixture" in call["scope_allowlist"]
    service._record_accesses.assert_not_called()
    service.store.record_accesses_batch.assert_not_called()
    service.store.record_access.assert_not_called()


class TestClassifyObservation:
    """Test observation pattern classification."""

    def test_classify_preference(self) -> None:
        """Preference pattern should match 'don't' and 'prefer'."""
        assert classify_observation("I prefer to use Python") == "preference"
        assert classify_observation("Don't use that library") == "preference"
        assert classify_observation("Please always test before shipping") == "preference"

    def test_classify_decision(self) -> None:
        """Decision pattern should match 'decided', 'will', etc."""
        assert classify_observation("We decided to use React") == "decision"
        assert classify_observation("Let's use PostgreSQL") == "decision"
        assert classify_observation("Going to migrate to TypeScript") == "decision"

    def test_classify_constraint(self) -> None:
        """Constraint pattern should match 'must', 'require', etc."""
        assert classify_observation("Must support Python 3.8+") == "constraint"
        assert classify_observation("Mandatory HTTPS for all endpoints") == "constraint"
        assert classify_observation("This is a critical requirement") == "constraint"

    def test_classify_fact(self) -> None:
        """Fact pattern should match architecture/tech choices."""
        assert classify_observation("Using FastAPI for this service") == "fact"
        assert classify_observation("Deployed to AWS") == "fact"
        assert classify_observation("Installed PostgreSQL 13") == "fact"

    def test_classify_event(self) -> None:
        """Event pattern should match bugs and crashes."""
        assert classify_observation("Fixed the auth bug") == "event"
        assert classify_observation("There is a bug in the code") == "event"
        assert classify_observation("ERROR in the system") == "event"

    def test_classify_commitment(self) -> None:
        """Commitment pattern should match todos and next steps."""
        assert classify_observation("TODO: refactor the auth module") == "commitment"
        assert classify_observation("Will do this next") == "commitment"
        assert classify_observation("I should refactor this later") == "commitment"

    def test_classify_none_for_irrelevant(self) -> None:
        """Irrelevant text should return None."""
        assert classify_observation("Hello world") is None
        assert classify_observation("This is just a greeting") is None
        assert classify_observation("123456") is None

    def test_classify_case_insensitive(self) -> None:
        """Pattern matching should be case-insensitive."""
        assert classify_observation("DECIDED to use TypeScript") == "decision"
        assert classify_observation("MuSt validate input") == "constraint"


class TestRecall:
    """Test memory recall function (mocked)."""

    @patch("memorymaster.core.service.MemoryService")
    def test_recall_with_results(self, mock_service_class: MagicMock) -> None:
        """Recall should return formatted context when claims are found."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        # Mock query_rows to return claim-like dicts
        mock_claim = MagicMock()
        mock_claim.text = "working on memorymaster project"
        mock_claim.id = 1
        mock_claim.status = "confirmed"
        mock_claim.scope = "project"
        mock_claim.visibility = "public"
        mock_claim.object_value = None
        mock_claim.subject = None
        mock_claim.predicate = None
        mock_claim.valid_from = None
        mock_claim.valid_until = None
        mock_service.query_rows.return_value = [
            {"claim": mock_claim, "lexical_score": 0.8, "confidence_score": 0.9},
        ]

        result = recall("what am I working on?", db_path=":memory:", skip_qdrant=True)

        assert "working on memorymaster project" in result
        assert result.startswith("# Memory Context")
        mock_service.query_rows.assert_called_once()

    @patch("memorymaster.core.service.MemoryService")
    def test_recall_no_results(self, mock_service_class: MagicMock) -> None:
        """Recall should return empty string when no claims found."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.query_rows.return_value = []

        result = recall("what am I working on?", db_path=":memory:", skip_qdrant=True)

        assert result == ""

    @patch("memorymaster.core.service.MemoryService")
    def test_recall_with_custom_budget(self, mock_service_class: MagicMock) -> None:
        """Recall should respect custom token budget."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.text = "a" * 200
        mock_claim.status = "confirmed"
        mock_claim.scope = "project"
        mock_claim.visibility = "public"
        mock_claim.object_value = None
        mock_claim.subject = None
        mock_claim.predicate = None
        mock_claim.valid_from = None
        mock_claim.valid_until = None
        mock_service.query_rows.return_value = [
            {"claim": mock_claim, "lexical_score": 0.5, "confidence_score": 0.5},
        ]

        result = recall("test query here", db_path=":memory:", budget=5000, skip_qdrant=True)

        assert "a" * 200 in result

    @patch("memorymaster.core.service.MemoryService")
    def test_prompt_hook_injects_at_most_two_of_many_candidates(self, mock_service_class: MagicMock, tmp_path) -> None:
        """The prompt hook (hook_data given) caps injection at 2 claims; other callers keep the full block (T-1351)."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        rows = []
        for n, word in enumerate(("alpha", "bravo", "charlie", "delta")):
            claim = MagicMock()
            claim.id = 100 + n
            claim.text = f"{word} deployment note number {n}"
            claim.status = "confirmed"
            claim.scope = "project"
            claim.visibility = "public"
            claim.object_value = None
            claim.subject = None
            claim.predicate = None
            claim.valid_from = None
            claim.valid_until = None
            rows.append({"claim": claim, "lexical_score": 0.9 - 0.1 * n, "confidence_score": 0.9})
        mock_service.query_rows.return_value = rows

        _, hook_ids = recall("deployment note", db_path=":memory:", return_ids=True,
                             hook_data={"cwd": str(tmp_path)})
        _, full_ids = recall("deployment note", db_path=":memory:", return_ids=True)

        assert len(hook_ids) == 2
        assert len(full_ids) == 4

    @patch("memorymaster.core.service.MemoryService")
    def test_recall_sanitizes_non_ascii(self, mock_service_class: MagicMock) -> None:
        """Recall should sanitize non-ASCII characters for Windows console."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.text = "claim with \u00f1 and \u00e9 characters"
        mock_claim.status = "confirmed"
        mock_claim.scope = "project"
        mock_claim.visibility = "public"
        mock_claim.object_value = None
        mock_claim.subject = None
        mock_claim.predicate = None
        mock_claim.valid_from = None
        mock_claim.valid_until = None
        mock_service.query_rows.return_value = [
            {"claim": mock_claim, "lexical_score": 0.5, "confidence_score": 0.5},
        ]

        result = recall("test query here", db_path=":memory:", skip_qdrant=True)

        # Non-ASCII chars should be replaced with ?
        assert "claim" in result


class TestObserve:
    """Test observation ingestion (mocked)."""

    @patch("memorymaster.core.service.MemoryService")
    def test_observe_with_pattern_match(self, mock_service_class: MagicMock) -> None:
        """Observe should ingest text that matches a pattern."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 42
        mock_service.ingest.return_value = mock_claim

        result = observe("We decided to use React", source="session")

        assert result["ingested"] is True
        assert result["claim_type"] == "decision"
        assert result["claim_id"] == 42

        mock_service.ingest.assert_called_once()
        call_kwargs = mock_service.ingest.call_args.kwargs
        assert call_kwargs["text"] == "We decided to use React"
        assert call_kwargs["claim_type"] == "decision"
        assert call_kwargs["source_agent"] == "context-hook"

    @patch("memorymaster.core.service.MemoryService")
    def test_observe_no_pattern_match_with_auto_classify(self, mock_service_class: MagicMock) -> None:
        """Observe should skip ingestion if text doesn't match pattern and auto_classify=True."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        result = observe("Just a random sentence", source="session", auto_classify=True)

        assert result["ingested"] is False
        assert result["claim_type"] is None
        assert result["reason"] == "no_pattern_match"
        mock_service.ingest.assert_not_called()

    @patch("memorymaster.core.service.MemoryService")
    def test_observe_force_ingest_without_pattern(self, mock_service_class: MagicMock) -> None:
        """Observe should ingest even without pattern match if force=True."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 99
        mock_service.ingest.return_value = mock_claim

        result = observe("Just a random sentence", source="session", force=True)

        assert result["ingested"] is True
        assert result["claim_type"] == "fact"
        assert result["claim_id"] == 99

    @patch("memorymaster.core.service.MemoryService")
    def test_observe_auto_classify_false(self, mock_service_class: MagicMock) -> None:
        """Observe should ingest regardless of pattern if auto_classify=False."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 77
        mock_service.ingest.return_value = mock_claim

        result = observe("Just a random sentence", source="session", auto_classify=False)

        assert result["ingested"] is True
        assert result["claim_type"] == "fact"
        assert result["claim_id"] == 77

    @patch("memorymaster.core.service.MemoryService")
    def test_observe_truncates_long_text(self, mock_service_class: MagicMock) -> None:
        """Observe should truncate text to 2000 chars."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 1
        mock_service.ingest.return_value = mock_claim

        long_text = "We decided to do X. " * 200  # ~3000 chars
        observe(long_text, source="session")

        call_kwargs = mock_service.ingest.call_args.kwargs
        assert len(call_kwargs["text"]) <= 2000

    @patch("memorymaster.core.service.MemoryService")
    def test_observe_handles_ingest_failure(self, mock_service_class: MagicMock) -> None:
        """Observe should handle exceptions during ingestion gracefully."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service
        mock_service.ingest.side_effect = ValueError("DB error")

        result = observe("We decided to use React", source="session")

        assert result["ingested"] is False
        assert "DB error" in result.get("reason", "")

    @patch("memorymaster.core.service.MemoryService")
    def test_observe_with_custom_scope(self, mock_service_class: MagicMock) -> None:
        """Observe should respect custom scope parameter."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 1
        mock_service.ingest.return_value = mock_claim

        observe("We decided to use React", source="session", scope="personal")

        call_kwargs = mock_service.ingest.call_args.kwargs
        assert call_kwargs["scope"] == "personal"

    @patch("memorymaster.core.service.MemoryService")
    def test_observe_sets_confidence(self, mock_service_class: MagicMock) -> None:
        """Observe should set confidence to 0.6."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        mock_claim = MagicMock()
        mock_claim.id = 1
        mock_service.ingest.return_value = mock_claim

        observe("We decided to use React", source="session")

        call_kwargs = mock_service.ingest.call_args.kwargs
        assert call_kwargs["confidence"] == 0.6


class TestFinishRecallStages:
    """T-1648: the render, Jev and access-spool stages report their own time without changing the block."""

    def test_stages_are_filled_and_the_block_is_the_same_with_or_without_them(self) -> None:
        from types import SimpleNamespace

        from memorymaster.recall.context_hook import _finish_recall

        claim = SimpleNamespace(id=7, text="netcode tick rate fixed at 60 Hz", human_id="mm-7")
        svc = MagicMock()
        kwargs = {"budget": 500, "svc": svc, "phase_ms": {}, "rank_start": 0.0, "hook_data": None, "rendered_ids": None}
        stages: dict[str, float] = {}
        with_stages = _finish_recall("tick rate", [{"claim": claim}], stages=stages, **kwargs)
        without = _finish_recall("tick rate", [{"claim": claim}], **kwargs)
        assert with_stages == without and "mm-7" in with_stages[0]
        assert set(stages) == {"render_ms", "remember_ms", "access_ms"} and all(v >= 0 for v in stages.values())
        svc._record_accesses.assert_called()


def test_whole_token_coverage_never_matches_a_substring():
    """T-1862 (review F8): "port" is not found in "report"; a plural folds onto its singular."""
    from memorymaster.recall import hook_lexical

    keys = hook_lexical.term_keys(["port", "redis", "deploy"])
    assert hook_lexical.matched_terms(keys, "Quarterly report on redis usage") == {hook_lexical.term_key("redis")}
    assert hook_lexical.matched_terms(keys, "the ports and deploys of redis") == keys
    assert not hook_lexical.covers(frozenset({"redis"}), 3, frozenset())  # one generic term of three
    assert hook_lexical.covers(frozenset({"redis"}), 3, frozenset({"redis"}))  # an exact identifier
    assert hook_lexical.covers(frozenset(), 0, frozenset())  # nothing to match: nothing gated


def test_truth_eligibility_drops_a_predecessor_whose_correction_is_in_the_pool(monkeypatch):
    """T-1862 (review F4): the correction stays, the claim it replaces does not precede it."""
    from memorymaster.recall import hook_lexical, retrieval

    monkeypatch.setattr(retrieval, "pending_supersession_ids", lambda _svc: frozenset({1, 3}))
    old = SimpleNamespace(id=1, supersedes_claim_id=None, replaced_by_claim_id=None)
    new = SimpleNamespace(id=2, supersedes_claim_id=1, replaced_by_claim_id=None)
    pending_only = SimpleNamespace(id=3, supersedes_claim_id=None, replaced_by_claim_id=None)
    other = SimpleNamespace(id=4, supersedes_claim_id=None, replaced_by_claim_id=None)
    rows = [{"claim": c} for c in (old, pending_only, new, other)]
    ids = [r["claim"].id for r in hook_lexical.apply_truth_eligibility(rows, MagicMock())]
    assert ids == [2, 4, 3]  # old dropped (correction present); pending-only sinks below undisputed rows


def test_truth_eligibility_fails_open(monkeypatch):
    from memorymaster.recall import hook_lexical, retrieval

    def boom(_svc):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(retrieval, "pending_supersession_ids", boom)
    rows = [{"claim": SimpleNamespace(id=1, supersedes_claim_id=None, replaced_by_claim_id=None)}]
    assert hook_lexical.apply_truth_eligibility(rows, MagicMock()) == rows
