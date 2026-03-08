"""Tests for memorymaster.conflict_resolver module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.models import Claim, Citation
from memorymaster.service import MemoryService
from memorymaster.conflict_resolver import (
    ConflictPair,
    ResolutionResult,
    _build_conflict_groups,
    _pick_winner,
    detect_conflicts,
    resolve_conflicts,
)


def _make_claim(
    *,
    id: int = 1,
    text: str = "test claim",
    subject: str | None = "server",
    predicate: str | None = "port",
    object_value: str | None = "8080",
    scope: str = "project",
    status: str = "confirmed",
    confidence: float = 0.7,
    pinned: bool = False,
    updated_at: str = "2026-01-01T00:00:00+00:00",
    citations: list | None = None,
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
        scope=scope,
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
        citations=citations or [],
    )


class TestPickWinner:
    def test_higher_confidence_wins(self):
        a = _make_claim(id=1, confidence=0.9)
        b = _make_claim(id=2, confidence=0.5)
        pair = _pick_winner(a, b)
        assert pair.winner.id == 1
        assert pair.loser.id == 2
        assert pair.reason == "higher_confidence"

    def test_lower_confidence_loses(self):
        a = _make_claim(id=1, confidence=0.3)
        b = _make_claim(id=2, confidence=0.8)
        pair = _pick_winner(a, b)
        assert pair.winner.id == 2
        assert pair.loser.id == 1
        assert pair.reason == "higher_confidence"

    def test_equal_confidence_more_recent_wins(self):
        a = _make_claim(id=1, confidence=0.7, updated_at="2026-01-01T00:00:00+00:00")
        b = _make_claim(id=2, confidence=0.7, updated_at="2026-02-01T00:00:00+00:00")
        pair = _pick_winner(a, b)
        assert pair.winner.id == 2
        assert pair.reason == "more_recent"

    def test_equal_confidence_and_time_more_citations_wins(self):
        cite = Citation(id=1, claim_id=1, source="test", locator=None, excerpt=None, created_at="2026-01-01T00:00:00+00:00")
        a = _make_claim(id=1, confidence=0.7, citations=[cite, cite])
        b = _make_claim(id=2, confidence=0.7, citations=[cite])
        pair = _pick_winner(a, b)
        assert pair.winner.id == 1
        assert pair.reason == "more_citations"

    def test_all_equal_higher_id_wins(self):
        a = _make_claim(id=1, confidence=0.7)
        b = _make_claim(id=2, confidence=0.7)
        pair = _pick_winner(a, b)
        assert pair.winner.id == 2
        assert pair.reason == "higher_id_tiebreaker"

    def test_pinned_wins_over_higher_confidence(self):
        a = _make_claim(id=1, confidence=0.3, pinned=True)
        b = _make_claim(id=2, confidence=0.9, pinned=False)
        pair = _pick_winner(a, b)
        assert pair.winner.id == 1
        assert pair.reason == "pinned_wins"

    def test_both_pinned_falls_through_to_confidence(self):
        a = _make_claim(id=1, confidence=0.9, pinned=True)
        b = _make_claim(id=2, confidence=0.5, pinned=True)
        pair = _pick_winner(a, b)
        assert pair.winner.id == 1
        assert pair.reason == "higher_confidence"


class TestBuildConflictGroups:
    def test_no_conflicts_same_value(self):
        a = _make_claim(id=1, object_value="8080")
        b = _make_claim(id=2, object_value="8080")
        groups = _build_conflict_groups([a, b])
        assert len(groups) == 0

    def test_conflict_different_values(self):
        a = _make_claim(id=1, object_value="8080")
        b = _make_claim(id=2, object_value="3000")
        groups = _build_conflict_groups([a, b])
        assert len(groups) == 1
        key = ("server", "port", "project")
        assert key in groups
        assert len(groups[key]) == 2

    def test_no_conflict_different_subjects(self):
        a = _make_claim(id=1, subject="server_a", object_value="8080")
        b = _make_claim(id=2, subject="server_b", object_value="3000")
        groups = _build_conflict_groups([a, b])
        assert len(groups) == 0

    def test_claims_without_subject_ignored(self):
        a = _make_claim(id=1, subject=None, object_value="8080")
        b = _make_claim(id=2, subject=None, object_value="3000")
        groups = _build_conflict_groups([a, b])
        assert len(groups) == 0

    def test_case_insensitive_value_match(self):
        a = _make_claim(id=1, object_value="True")
        b = _make_claim(id=2, object_value="true")
        groups = _build_conflict_groups([a, b])
        # Same value after normalization -> no conflict
        assert len(groups) == 0

    def test_single_claim_no_group(self):
        a = _make_claim(id=1)
        groups = _build_conflict_groups([a])
        assert len(groups) == 0


def _fresh_service(tmp_path: Path) -> MemoryService:
    db_path = tmp_path / "test.db"
    svc = MemoryService(str(db_path))
    svc.init_db()
    return svc


class TestDetectConflictsIntegration:
    def test_detects_conflict_from_store(self, tmp_path):
        svc = _fresh_service(tmp_path)
        from memorymaster.models import CitationInput

        svc.ingest(
            text="server port is 8080",
            citations=[CitationInput(source="doc1")],
            subject="server",
            predicate="port",
            object_value="8080",
            confidence=0.7,
        )
        svc.ingest(
            text="server port is 3000",
            citations=[CitationInput(source="doc2")],
            subject="server",
            predicate="port",
            object_value="3000",
            confidence=0.9,
        )
        pairs = detect_conflicts(svc.store, statuses=["candidate"])
        assert len(pairs) >= 1
        pair = pairs[0]
        assert pair.winner.object_value != pair.loser.object_value

    def test_no_conflict_same_value(self, tmp_path):
        svc = _fresh_service(tmp_path)
        from memorymaster.models import CitationInput

        svc.ingest(
            text="server port is 8080",
            citations=[CitationInput(source="doc1")],
            subject="server",
            predicate="port",
            object_value="8080",
            confidence=0.7,
        )
        svc.ingest(
            text="server port is also 8080",
            citations=[CitationInput(source="doc2")],
            subject="server",
            predicate="port",
            object_value="8080",
            confidence=0.9,
        )
        pairs = detect_conflicts(svc.store, statuses=["candidate"])
        assert len(pairs) == 0


class TestResolveConflictsIntegration:
    def test_dry_run_no_transitions(self, tmp_path):
        svc = _fresh_service(tmp_path)
        from memorymaster.models import CitationInput

        c1 = svc.ingest(
            text="server port is 8080",
            citations=[CitationInput(source="doc1")],
            subject="server",
            predicate="port",
            object_value="8080",
            confidence=0.7,
        )
        c2 = svc.ingest(
            text="server port is 3000",
            citations=[CitationInput(source="doc2")],
            subject="server",
            predicate="port",
            object_value="3000",
            confidence=0.9,
        )
        result = resolve_conflicts(svc, dry_run=True, statuses=["candidate"])
        assert result.pairs_detected >= 1
        assert result.pairs_resolved == 0
        assert result.pairs_skipped >= 1

        # Verify no claim was actually changed
        fresh_c1 = svc.store.get_claim(c1.id)
        fresh_c2 = svc.store.get_claim(c2.id)
        assert fresh_c1.status == "candidate"
        assert fresh_c2.status == "candidate"

    def test_resolve_applies_superseded(self, tmp_path):
        svc = _fresh_service(tmp_path)
        from memorymaster.models import CitationInput

        c1 = svc.ingest(
            text="server port is 8080",
            citations=[CitationInput(source="doc1")],
            subject="server",
            predicate="port",
            object_value="8080",
            confidence=0.5,
        )
        c2 = svc.ingest(
            text="server port is 3000",
            citations=[CitationInput(source="doc2")],
            subject="server",
            predicate="port",
            object_value="3000",
            confidence=0.9,
        )
        result = resolve_conflicts(svc, dry_run=False, statuses=["candidate"])
        assert result.pairs_detected >= 1
        assert result.pairs_resolved >= 1

        # Higher confidence claim wins
        winner = svc.store.get_claim(c2.id)
        loser = svc.store.get_claim(c1.id)
        assert winner.status == "candidate"  # winner stays unchanged
        assert loser.status == "superseded"
        assert loser.replaced_by_claim_id == c2.id

    def test_pinned_loser_skipped(self, tmp_path):
        svc = _fresh_service(tmp_path)
        from memorymaster.models import CitationInput

        c1 = svc.ingest(
            text="server port is 8080",
            citations=[CitationInput(source="doc1")],
            subject="server",
            predicate="port",
            object_value="8080",
            confidence=0.5,
        )
        # Pin the low-confidence claim
        svc.pin(c1.id, pin=True)

        c2 = svc.ingest(
            text="server port is 3000",
            citations=[CitationInput(source="doc2")],
            subject="server",
            predicate="port",
            object_value="3000",
            confidence=0.9,
        )
        result = resolve_conflicts(svc, dry_run=False, statuses=["candidate"])
        assert result.pairs_detected >= 1
        # The pinned claim wins (even with lower confidence), and the loser
        # is the unpinned c2. But c2 is unpinned so it should be superseded.
        # Actually: _pick_winner gives pinned c1 the win. c2 is the loser.
        # c2 is not pinned, so it should get superseded.
        for res in result.resolutions:
            if res.get("applied"):
                assert res["winner_id"] == c1.id
                assert res["loser_id"] == c2.id

    def test_already_superseded_skipped(self, tmp_path):
        svc = _fresh_service(tmp_path)
        from memorymaster.models import CitationInput

        c1 = svc.ingest(
            text="server port is 8080",
            citations=[CitationInput(source="doc1")],
            subject="server",
            predicate="port",
            object_value="8080",
            confidence=0.5,
        )
        c2 = svc.ingest(
            text="server port is 3000",
            citations=[CitationInput(source="doc2")],
            subject="server",
            predicate="port",
            object_value="3000",
            confidence=0.9,
        )
        # First resolve
        resolve_conflicts(svc, dry_run=False, statuses=["candidate"])
        # Second resolve should skip the already-superseded claim
        result2 = resolve_conflicts(svc, dry_run=False, statuses=["candidate", "superseded"])
        # The pair is still detected, but the loser is already superseded
        assert result2.pairs_resolved == 0

    def test_resolution_creates_audit_event(self, tmp_path):
        svc = _fresh_service(tmp_path)
        from memorymaster.models import CitationInput

        c1 = svc.ingest(
            text="server port is 8080",
            citations=[CitationInput(source="doc1")],
            subject="server",
            predicate="port",
            object_value="8080",
            confidence=0.5,
        )
        c2 = svc.ingest(
            text="server port is 3000",
            citations=[CitationInput(source="doc2")],
            subject="server",
            predicate="port",
            object_value="3000",
            confidence=0.9,
        )
        resolve_conflicts(svc, dry_run=False, statuses=["candidate"])

        # Check the loser has audit events
        events = svc.list_events(claim_id=c1.id, event_type="policy_decision")
        conflict_events = [
            e for e in events
            if e.details == "conflict_auto_resolution"
        ]
        assert len(conflict_events) >= 1
        payload = json.loads(conflict_events[0].payload_json)
        assert payload["source"] == "conflict_resolver"
        assert payload["winner_id"] == c2.id

    def test_three_way_conflict(self, tmp_path):
        """Three claims with same tuple but different values - only one winner."""
        svc = _fresh_service(tmp_path)
        from memorymaster.models import CitationInput

        c1 = svc.ingest(
            text="server port is 8080",
            citations=[CitationInput(source="doc1")],
            subject="server",
            predicate="port",
            object_value="8080",
            confidence=0.5,
        )
        c2 = svc.ingest(
            text="server port is 3000",
            citations=[CitationInput(source="doc2")],
            subject="server",
            predicate="port",
            object_value="3000",
            confidence=0.7,
        )
        c3 = svc.ingest(
            text="server port is 443",
            citations=[CitationInput(source="doc3")],
            subject="server",
            predicate="port",
            object_value="443",
            confidence=0.9,
        )
        result = resolve_conflicts(svc, dry_run=False, statuses=["candidate"])
        assert result.pairs_detected == 2  # c3 vs c2, c3 vs c1
        # c3 should win (highest confidence)
        c3_fresh = svc.store.get_claim(c3.id)
        assert c3_fresh.status == "candidate"


class TestCLIResolveConflicts:
    def test_cli_resolve_conflicts_dry_run(self, tmp_path):
        from memorymaster.cli import main

        db = str(tmp_path / "test.db")
        assert main(["--db", db, "init-db"]) == 0
        assert main([
            "--db", db, "ingest",
            "--text", "port is 8080",
            "--source", "doc1",
            "--subject", "server",
            "--predicate", "port",
            "--object", "8080",
        ]) == 0
        assert main([
            "--db", db, "ingest",
            "--text", "port is 3000",
            "--source", "doc2",
            "--subject", "server",
            "--predicate", "port",
            "--object", "3000",
        ]) == 0
        rc = main(["--db", db, "resolve-conflicts", "--dry-run"])
        assert rc == 0

    def test_cli_resolve_conflicts_json(self, tmp_path, capsys):
        from memorymaster.cli import main

        db = str(tmp_path / "test.db")
        assert main(["--db", db, "init-db"]) == 0
        assert main([
            "--db", db, "ingest",
            "--text", "port is 8080",
            "--source", "doc1",
            "--subject", "server",
            "--predicate", "port",
            "--object", "8080",
        ]) == 0
        assert main([
            "--db", db, "ingest",
            "--text", "port is 3000",
            "--source", "doc2",
            "--subject", "server",
            "--predicate", "port",
            "--object", "3000",
            "--confidence", "0.9",
        ]) == 0
        # Clear captured output from setup commands
        capsys.readouterr()
        rc = main(["--json", "--db", db, "resolve-conflicts"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ok"] is True
        assert data["data"]["pairs_detected"] >= 1
        assert data["data"]["pairs_resolved"] >= 1
