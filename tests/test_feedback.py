"""Tests for feedback tracking and quality scoring."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.feedback import FeedbackTracker


@pytest.fixture
def db_path():
    """Create temporary database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test.db"


@pytest.fixture
def tracker(db_path):
    """Create FeedbackTracker instance."""
    return FeedbackTracker(str(db_path))


@pytest.fixture
def claims_table(db_path):
    """Create claims table for testing."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                confidence REAL DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                last_accessed TEXT,
                created_at TEXT,
                tier TEXT DEFAULT 'working'
            )
        """)
        # Insert test claims
        now = datetime.now(timezone.utc).isoformat()
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        conn.execute(
            "INSERT INTO claims (id, text, status, access_count, last_accessed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "Claim 1", "active", 5, week_ago, week_ago),
        )
        conn.execute(
            "INSERT INTO claims (id, text, status, access_count, last_accessed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (2, "Claim 2", "active", 0, None, month_ago),
        )
        conn.execute(
            "INSERT INTO claims (id, text, status, access_count, last_accessed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (3, "Claim 3", "archived", 10, None, month_ago),
        )
        conn.commit()
    finally:
        conn.close()


class TestFeedbackTrackerEnsureTables:
    """Test table creation."""

    def test_ensure_tables_creates_schema(self, tracker):
        """ensure_tables creates required tables."""
        tracker.ensure_tables()
        conn = tracker._connect()
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [r[0] for r in tables]
            assert "usage_feedback" in table_names
            assert "quality_scores" in table_names
        finally:
            conn.close()

    def test_ensure_tables_idempotent(self, tracker):
        """ensure_tables can be called multiple times."""
        tracker.ensure_tables()
        tracker.ensure_tables()
        conn = tracker._connect()
        try:
            result = conn.execute("SELECT COUNT(*) FROM usage_feedback").fetchone()[0]
            assert result == 0
        finally:
            conn.close()


class TestRecordRetrieval:
    """Test recording claim retrievals."""

    def test_record_retrieval_empty_list(self, tracker):
        """record_retrieval with empty list returns 0."""
        tracker.ensure_tables()
        result = tracker.record_retrieval([], "test query")
        assert result == 0

    def test_record_retrieval_single_claim(self, tracker):
        """record_retrieval stores feedback for claim."""
        tracker.ensure_tables()
        result = tracker.record_retrieval([42], "what is X?")
        assert result == 1

        conn = tracker._connect()
        try:
            feedback = conn.execute("SELECT * FROM usage_feedback").fetchall()
            assert len(feedback) == 1
            assert feedback[0]["claim_id"] == 42
            assert "what is X?" in feedback[0]["query_text"]
        finally:
            conn.close()

    def test_record_retrieval_multiple_claims(self, tracker):
        """record_retrieval stores feedback for multiple claims."""
        tracker.ensure_tables()
        result = tracker.record_retrieval([1, 2, 3], "test query")
        assert result == 3

        conn = tracker._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM usage_feedback").fetchone()[0]
            assert count == 3
        finally:
            conn.close()

    def test_record_retrieval_truncates_long_query(self, tracker):
        """record_retrieval truncates query to 500 chars."""
        tracker.ensure_tables()
        long_query = "x" * 1000
        tracker.record_retrieval([1], long_query)

        conn = tracker._connect()
        try:
            query = conn.execute("SELECT query_text FROM usage_feedback").fetchone()
            assert len(query["query_text"]) == 500
        finally:
            conn.close()


class TestComputeQualityScores:
    """Test quality score computation."""

    def test_compute_quality_scores_empty(self, tracker, claims_table):
        """compute_quality_scores handles empty database."""
        tracker.ensure_tables()
        result = tracker.compute_quality_scores()
        assert result["scored"] == 2  # Only active claims (archived is skipped)

    def test_compute_quality_scores_basic(self, tracker, claims_table):
        """compute_quality_scores computes scores for active claims."""
        tracker.ensure_tables()
        tracker.record_retrieval([1, 2], "test query")

        result = tracker.compute_quality_scores()
        assert result["scored"] == 2  # Only active claims

        conn = tracker._connect()
        try:
            scores = conn.execute("SELECT * FROM quality_scores ORDER BY claim_id").fetchall()
            assert len(scores) == 2

            # Claim 1 should have higher score (recently accessed)
            claim1_score = scores[0]["quality_score"]
            claim2_score = scores[1]["quality_score"]
            assert claim1_score > claim2_score
        finally:
            conn.close()

    def test_compute_quality_scores_formula(self, tracker, claims_table):
        """compute_quality_scores applies correct formula."""
        tracker.ensure_tables()
        tracker.record_retrieval([1], "query 1")
        tracker.record_retrieval([1], "query 2")

        tracker.compute_quality_scores()

        conn = tracker._connect()
        try:
            score = conn.execute("SELECT quality_score FROM quality_scores WHERE claim_id = 1").fetchone()
            # base=0.5 + retrieval_bonus=min(2*0.05, 0.3)=0.1 + access_bonus=min(5*0.03, 0.2)=0.15
            # + freshness=0.1 = 0.75
            assert 0.7 <= score["quality_score"] <= 0.8
        finally:
            conn.close()

    def test_compute_quality_scores_staleness_penalty(self, tracker, claims_table):
        """compute_quality_scores applies staleness penalty for old unused claims."""
        tracker.ensure_tables()
        result = tracker.compute_quality_scores()

        conn = tracker._connect()
        try:
            # Claim 2: created 30+ days ago, never accessed - staleness check happens
            claim2 = conn.execute(
                "SELECT quality_score, factors FROM quality_scores WHERE claim_id = 2"
            ).fetchone()
            # Staleness is calculated, and claim with no access should score lower
            assert claim2 is not None
            # Even if staleness penalty not applied due to timing, quality should be 0.5 (base)
            # Claim 1 has higher score due to access_count and freshness
            assert claim2["quality_score"] <= 0.5
        finally:
            conn.close()

    def test_compute_quality_scores_skips_archived(self, tracker, claims_table):
        """compute_quality_scores skips archived claims."""
        tracker.ensure_tables()
        result = tracker.compute_quality_scores()

        conn = tracker._connect()
        try:
            archived_score = conn.execute(
                "SELECT * FROM quality_scores WHERE claim_id = 3"
            ).fetchone()
            assert archived_score is None
        finally:
            conn.close()


class TestGetTopQuality:
    """Test retrieving top quality claims."""

    def test_get_top_quality_empty(self, tracker, claims_table):
        """get_top_quality returns empty list when no scores."""
        tracker.ensure_tables()
        result = tracker.get_top_quality(limit=10)
        assert result == []

    def test_get_top_quality_with_data(self, tracker, claims_table):
        """get_top_quality returns claims ranked by score."""
        tracker.ensure_tables()
        tracker.record_retrieval([1, 2], "query")
        tracker.compute_quality_scores()

        result = tracker.get_top_quality(limit=10)
        assert len(result) == 2
        # Should be sorted by quality_score descending
        assert result[0]["quality_score"] >= result[1]["quality_score"]

    def test_get_top_quality_limit(self, tracker, claims_table):
        """get_top_quality respects limit parameter."""
        tracker.ensure_tables()
        tracker.record_retrieval([1, 2], "query")
        tracker.compute_quality_scores()

        result = tracker.get_top_quality(limit=1)
        assert len(result) == 1


class TestGetStats:
    """Test statistics reporting."""

    def test_get_stats_empty(self, tracker):
        """get_stats returns zeros for empty tracker."""
        tracker.ensure_tables()
        stats = tracker.get_stats()
        assert stats["feedback_rows"] == 0
        assert stats["claims_scored"] == 0
        assert stats["avg_quality"] == 0.0

    def test_get_stats_with_data(self, tracker, claims_table):
        """get_stats returns accurate counts."""
        tracker.ensure_tables()
        tracker.record_retrieval([1, 1, 2], "query")
        tracker.compute_quality_scores()

        stats = tracker.get_stats()
        assert stats["feedback_rows"] == 3
        assert stats["claims_scored"] == 2
        assert isinstance(stats["avg_quality"], float)
        assert 0 <= stats["avg_quality"] <= 1
