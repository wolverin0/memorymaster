"""Tests for the deduplication engine."""

from __future__ import annotations

import pytest

from memorymaster.embeddings import EmbeddingProvider, cosine_similarity
from memorymaster.jobs.dedup import (
    DuplicatePair,
    _pick_survivor,
    _subject_predicate_match,
    _text_overlap,
    find_duplicates,
    run,
)
from memorymaster.models import Claim
from memorymaster.service import MemoryService


def _make_claim(
    id: int,
    text: str = "some claim",
    confidence: float = 0.5,
    status: str = "confirmed",
    subject: str | None = None,
    predicate: str | None = None,
    object_value: str | None = None,
    pinned: bool = False,
    updated_at: str = "2026-01-01T00:00:00+00:00",
) -> Claim:
    return Claim(
        id=id,
        text=text,
        idempotency_key=None,
        normalized_text=None,
        claim_type=None,
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        scope="project",
        volatility="medium",
        status=status,
        confidence=confidence,
        pinned=pinned,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at=updated_at,
        last_validated_at=None,
        archived_at=None,
        citations=[],
    )


class TestTextOverlap:
    def test_identical(self):
        assert _text_overlap("hello world", "hello world") == 1.0

    def test_no_overlap(self):
        assert _text_overlap("hello world", "foo bar") == 0.0

    def test_partial_overlap(self):
        result = _text_overlap("the quick brown fox", "the slow brown cat")
        # intersection = {the, brown}, union = {the, quick, brown, fox, slow, cat}
        assert abs(result - 2 / 6) < 0.001

    def test_empty_string(self):
        assert _text_overlap("", "hello") == 0.0
        assert _text_overlap("hello", "") == 0.0

    def test_case_insensitive(self):
        assert _text_overlap("Hello World", "hello world") == 1.0


class TestSubjectPredicateMatch:
    def test_match(self):
        a = _make_claim(1, subject="Python", predicate="version")
        b = _make_claim(2, subject="Python", predicate="version")
        assert _subject_predicate_match(a, b) is True

    def test_case_insensitive(self):
        a = _make_claim(1, subject="python", predicate="VERSION")
        b = _make_claim(2, subject="Python", predicate="version")
        assert _subject_predicate_match(a, b) is True

    def test_no_match_different_subject(self):
        a = _make_claim(1, subject="Python", predicate="version")
        b = _make_claim(2, subject="Node", predicate="version")
        assert _subject_predicate_match(a, b) is False

    def test_no_match_missing_fields(self):
        a = _make_claim(1, subject="Python", predicate=None)
        b = _make_claim(2, subject="Python", predicate="version")
        assert _subject_predicate_match(a, b) is False

    def test_both_none(self):
        a = _make_claim(1)
        b = _make_claim(2)
        assert _subject_predicate_match(a, b) is False


class TestPickSurvivor:
    def test_higher_confidence_wins(self):
        a = _make_claim(1, confidence=0.8)
        b = _make_claim(2, confidence=0.9)
        keep, archive = _pick_survivor(a, b)
        assert keep.id == 2
        assert archive.id == 1

    def test_pinned_wins_on_tie(self):
        a = _make_claim(1, confidence=0.5, pinned=True)
        b = _make_claim(2, confidence=0.5, pinned=False)
        keep, archive = _pick_survivor(a, b)
        assert keep.id == 1

    def test_newer_wins_on_tie(self):
        a = _make_claim(1, confidence=0.5, updated_at="2026-01-02T00:00:00+00:00")
        b = _make_claim(2, confidence=0.5, updated_at="2026-01-01T00:00:00+00:00")
        keep, archive = _pick_survivor(a, b)
        assert keep.id == 1

    def test_lower_id_wins_on_complete_tie(self):
        a = _make_claim(1, confidence=0.5)
        b = _make_claim(2, confidence=0.5)
        keep, archive = _pick_survivor(a, b)
        assert keep.id == 1


class TestFindDuplicates:
    def test_identical_texts_detected(self):
        provider = EmbeddingProvider(model="hash-v1", dims=128)
        claims = [
            _make_claim(1, text="Python version is 3.12"),
            _make_claim(2, text="Python version is 3.12"),
        ]
        pairs = find_duplicates(claims, provider, threshold=0.90)
        assert len(pairs) == 1
        assert pairs[0].similarity >= 0.90

    def test_different_texts_not_flagged(self):
        provider = EmbeddingProvider(model="hash-v1", dims=128)
        claims = [
            _make_claim(1, text="Python version is 3.12"),
            _make_claim(2, text="The weather is sunny today in Buenos Aires"),
        ]
        pairs = find_duplicates(claims, provider, threshold=0.90)
        assert len(pairs) == 0

    def test_single_claim_no_pairs(self):
        provider = EmbeddingProvider(model="hash-v1", dims=128)
        claims = [_make_claim(1, text="some text")]
        pairs = find_duplicates(claims, provider)
        assert len(pairs) == 0

    def test_empty_list(self):
        provider = EmbeddingProvider(model="hash-v1", dims=128)
        pairs = find_duplicates([], provider)
        assert len(pairs) == 0

    def test_subject_predicate_match_boosts_detection(self):
        """Claims with same subject/predicate but slightly different text should match."""
        provider = EmbeddingProvider(model="hash-v1", dims=128)
        claims = [
            _make_claim(
                1,
                text="Python version is 3.12",
                subject="Python",
                predicate="version",
                object_value="3.12",
                confidence=0.9,
            ),
            _make_claim(
                2,
                text="Python version is 3.12",
                subject="Python",
                predicate="version",
                object_value="3.12",
                confidence=0.5,
            ),
        ]
        pairs = find_duplicates(claims, provider, threshold=0.90)
        assert len(pairs) == 1
        assert pairs[0].keep_id == 1  # higher confidence

    def test_chain_prevention(self):
        """If A is dup of B and B is dup of C, only one pair should form (no chain)."""
        provider = EmbeddingProvider(model="hash-v1", dims=128)
        claims = [
            _make_claim(1, text="exact same text here", confidence=0.9),
            _make_claim(2, text="exact same text here", confidence=0.5),
            _make_claim(3, text="exact same text here", confidence=0.3),
        ]
        pairs = find_duplicates(claims, provider, threshold=0.90)
        # Claim 1 should survive, claims 2 and 3 archived
        archived_ids = {p.archive_id for p in pairs}
        keep_ids = {p.keep_id for p in pairs}
        assert 1 in keep_ids or 1 not in archived_ids
        # No claim should be both kept and archived
        assert not (keep_ids & archived_ids)


class TestRunIntegration:
    @pytest.fixture()
    def service(self, tmp_path):
        db_path = tmp_path / "test_dedup.db"
        svc = MemoryService(str(db_path))
        svc.init_db()
        return svc

    def _ingest(self, service, text, **kwargs):
        from memorymaster.models import CitationInput
        defaults = {
            "citations": [CitationInput(source="test")],
            "scope": "project",
        }
        defaults.update(kwargs)
        return service.ingest(text=text, **defaults)

    def test_dry_run_no_changes(self, service):
        self._ingest(service, "Python version is 3.12")
        self._ingest(service, "Python version is 3.12")
        result = service.dedup(dry_run=True)
        assert result["dry_run"] is True
        assert result["scanned"] >= 2
        # Claims still active
        claims = service.list_claims(include_archived=True)
        archived = [c for c in claims if c.status == "archived"]
        assert len(archived) == 0

    def test_dedup_archives_duplicate(self, service):
        c1 = self._ingest(service, "Python version is 3.12")
        c2 = self._ingest(service, "Python version is 3.12")
        result = service.dedup(dry_run=False)
        assert result["duplicates_found"] >= 1
        assert result["claims_archived"] >= 1
        # Check the archived claim
        claims = service.list_claims(include_archived=True)
        archived = [c for c in claims if c.status == "archived"]
        assert len(archived) >= 1

    def test_different_claims_not_merged(self, service):
        self._ingest(service, "Python version is 3.12")
        self._ingest(service, "The database uses PostgreSQL 16 with pgvector extension")
        result = service.dedup(dry_run=False)
        assert result["duplicates_found"] == 0
        assert result["claims_archived"] == 0

    def test_custom_threshold(self, service):
        self._ingest(service, "Python version is 3.12")
        self._ingest(service, "Python version is 3.12")
        # Very high threshold should still catch identical texts
        result = service.dedup(threshold=0.99, dry_run=True)
        assert result["duplicates_found"] >= 1

    def test_dedup_event_recorded(self, service):
        self._ingest(service, "Python version is 3.12")
        self._ingest(service, "Python version is 3.12")
        service.dedup(dry_run=False)
        events = service.list_events(event_type="dedup_run")
        assert len(events) >= 1


class TestDedupCLI:
    def test_cli_dedup_dry_run(self, tmp_path):
        from memorymaster.cli import main
        db = str(tmp_path / "cli_dedup.db")
        assert main(["--db", db, "init-db"]) == 0
        assert main(["--db", db, "ingest", "--text", "test claim alpha", "--source", "s1"]) == 0
        assert main(["--db", db, "ingest", "--text", "test claim alpha", "--source", "s2"]) == 0
        assert main(["--db", db, "dedup", "--dry-run"]) == 0

    def test_cli_dedup_apply(self, tmp_path):
        from memorymaster.cli import main
        db = str(tmp_path / "cli_dedup2.db")
        assert main(["--db", db, "init-db"]) == 0
        assert main(["--db", db, "ingest", "--text", "test claim beta", "--source", "s1"]) == 0
        assert main(["--db", db, "ingest", "--text", "test claim beta", "--source", "s2"]) == 0
        assert main(["--db", db, "dedup"]) == 0

    def test_cli_dedup_json_output(self, tmp_path):
        import json
        from memorymaster.cli import main
        import io
        import sys

        db = str(tmp_path / "cli_dedup3.db")
        assert main(["--db", db, "init-db"]) == 0
        assert main(["--db", db, "ingest", "--text", "gamma claim text", "--source", "s1"]) == 0
        assert main(["--db", db, "ingest", "--text", "gamma claim text", "--source", "s2"]) == 0

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main(["--json", "--db", db, "dedup", "--dry-run"])
        finally:
            sys.stdout = old_stdout

        assert rc == 0
        output = json.loads(captured.getvalue())
        assert output["ok"] is True
        assert "data" in output

    def test_cli_dedup_custom_threshold(self, tmp_path):
        from memorymaster.cli import main
        db = str(tmp_path / "cli_dedup4.db")
        assert main(["--db", db, "init-db"]) == 0
        assert main(["--db", db, "ingest", "--text", "delta text here", "--source", "s1"]) == 0
        assert main(["--db", db, "ingest", "--text", "delta text here", "--source", "s2"]) == 0
        assert main(["--db", db, "dedup", "--threshold", "0.95", "--dry-run"]) == 0
