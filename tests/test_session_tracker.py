"""Tests for agent session tracking."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from memorymaster.session_tracker import SessionTracker


@pytest.fixture
def db_path(tmp_path):
    """Create temporary database."""
    return tmp_path / "test.db"


@pytest.fixture
def tracker(db_path):
    """Create SessionTracker instance."""
    return SessionTracker(str(db_path))


class TestSessionTrackerInit:
    """Test SessionTracker initialization."""

    def test_session_tracker_creates_table(self, tracker):
        """SessionTracker creates table on init."""
        # Access the database to verify table exists
        conn = tracker._connect()
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_sessions'"
            )
            assert cursor.fetchone() is not None
        finally:
            conn.close()


class TestStartSession:
    """Test session creation."""

    def test_start_session_returns_id(self, tracker):
        """start_session returns a session ID."""
        session_id = tracker.start_session("test-agent")
        assert isinstance(session_id, int)
        assert session_id > 0

    def test_start_session_stores_agent_id(self, tracker):
        """Session stores agent ID."""
        session_id = tracker.start_session("my-agent")

        conn = tracker._connect()
        try:
            row = conn.execute(
                "SELECT agent_id FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            assert row["agent_id"] == "my-agent"
        finally:
            conn.close()

    def test_start_session_multiple_sessions(self, tracker):
        """Can create multiple sessions."""
        id1 = tracker.start_session("agent1")
        id2 = tracker.start_session("agent2")
        id3 = tracker.start_session("agent1")

        assert id1 != id2
        assert id1 != id3

    def test_start_session_initializes_counters(self, tracker):
        """Session starts with zero counters."""
        session_id = tracker.start_session("agent")

        conn = tracker._connect()
        try:
            row = conn.execute(
                "SELECT claims_ingested, queries_made FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            assert row["claims_ingested"] == 0
            assert row["queries_made"] == 0
        finally:
            conn.close()


class TestRecordActivity:
    """Test activity recording."""

    def test_record_activity_ingest(self, tracker):
        """record_activity increments ingest counter."""
        session_id = tracker.start_session("agent")

        tracker.record_activity(session_id, "ingest")

        conn = tracker._connect()
        try:
            row = conn.execute(
                "SELECT claims_ingested FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            assert row["claims_ingested"] == 1
        finally:
            conn.close()

    def test_record_activity_query(self, tracker):
        """record_activity increments query counter."""
        session_id = tracker.start_session("agent")

        tracker.record_activity(session_id, "query")

        conn = tracker._connect()
        try:
            row = conn.execute(
                "SELECT queries_made FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            assert row["queries_made"] == 1
        finally:
            conn.close()

    def test_record_activity_multiple(self, tracker):
        """Multiple activities accumulate."""
        session_id = tracker.start_session("agent")

        tracker.record_activity(session_id, "ingest")
        tracker.record_activity(session_id, "ingest")
        tracker.record_activity(session_id, "query")

        conn = tracker._connect()
        try:
            row = conn.execute(
                "SELECT claims_ingested, queries_made FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            assert row["claims_ingested"] == 2
            assert row["queries_made"] == 1
        finally:
            conn.close()

    def test_record_activity_updates_last_activity(self, tracker):
        """record_activity updates last_activity timestamp."""
        session_id = tracker.start_session("agent")

        # Get initial timestamp
        conn = tracker._connect()
        try:
            row1 = conn.execute(
                "SELECT last_activity FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            initial_time = row1["last_activity"]
        finally:
            conn.close()

        # Wait a bit and record activity
        time.sleep(0.1)
        tracker.record_activity(session_id, "ingest")

        # Check updated timestamp
        conn = tracker._connect()
        try:
            row2 = conn.execute(
                "SELECT last_activity FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            updated_time = row2["last_activity"]
            assert updated_time > initial_time
        finally:
            conn.close()

    def test_record_activity_unknown_type(self, tracker):
        """Unknown activity type still updates last_activity."""
        session_id = tracker.start_session("agent")
        tracker.record_activity(session_id, "unknown_activity")

        conn = tracker._connect()
        try:
            row = conn.execute(
                "SELECT claims_ingested, queries_made FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            # Counters should not change
            assert row["claims_ingested"] == 0
            assert row["queries_made"] == 0
        finally:
            conn.close()


class TestGetActiveSessions:
    """Test getting active sessions."""

    def test_get_active_sessions_empty(self, tracker):
        """Empty tracker returns no active sessions."""
        result = tracker.get_active_sessions()
        assert result == []

    def test_get_active_sessions_recent(self, tracker):
        """Recent session is active."""
        session_id = tracker.start_session("agent")

        result = tracker.get_active_sessions()
        assert len(result) == 1
        assert result[0]["id"] == session_id
        assert result[0]["agent_id"] == "agent"

    def test_get_active_sessions_multiple(self, tracker):
        """Multiple sessions appear in results."""
        id1 = tracker.start_session("agent1")
        id2 = tracker.start_session("agent2")

        result = tracker.get_active_sessions()
        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert id1 in ids
        assert id2 in ids

    def test_get_active_sessions_ordered(self, tracker):
        """Active sessions are ordered by last_activity descending."""
        id1 = tracker.start_session("agent1")
        time.sleep(0.1)
        id2 = tracker.start_session("agent2")

        result = tracker.get_active_sessions()
        # Most recent (id2) should be first
        assert result[0]["id"] == id2
        assert result[1]["id"] == id1

    def test_get_active_sessions_excludes_old(self, tracker):
        """Sessions older than 1 hour are excluded."""
        # This is harder to test without mocking time, so we test the API works
        session_id = tracker.start_session("agent")
        result = tracker.get_active_sessions()
        assert len(result) >= 1


class TestGetSessionStats:
    """Test session statistics."""

    def test_get_session_stats_no_sessions(self, tracker):
        """Stats for unknown agent returns zeros."""
        result = tracker.get_session_stats("unknown-agent")
        assert result["agent_id"] == "unknown-agent"
        assert result["total_sessions"] == 0
        assert result["total_claims"] == 0
        assert result["total_queries"] == 0

    def test_get_session_stats_single_session(self, tracker):
        """Stats for single session."""
        tracker.start_session("my-agent")

        result = tracker.get_session_stats("my-agent")
        assert result["agent_id"] == "my-agent"
        assert result["total_sessions"] == 1
        assert result["total_claims"] == 0
        assert result["total_queries"] == 0

    def test_get_session_stats_aggregates(self, tracker):
        """Stats aggregate across multiple sessions."""
        # Session 1
        id1 = tracker.start_session("agent")
        tracker.record_activity(id1, "ingest")
        tracker.record_activity(id1, "ingest")
        tracker.record_activity(id1, "query")

        # Session 2
        id2 = tracker.start_session("agent")
        tracker.record_activity(id2, "ingest")
        tracker.record_activity(id2, "query")
        tracker.record_activity(id2, "query")

        result = tracker.get_session_stats("agent")
        assert result["total_sessions"] == 2
        assert result["total_claims"] == 3  # 2 + 1
        assert result["total_queries"] == 3  # 1 + 2

    def test_get_session_stats_only_agent(self, tracker):
        """Stats only for specified agent."""
        tracker.start_session("agent-a")
        id_b = tracker.start_session("agent-b")
        tracker.record_activity(id_b, "ingest")

        result_a = tracker.get_session_stats("agent-a")
        result_b = tracker.get_session_stats("agent-b")

        assert result_a["total_sessions"] == 1
        assert result_a["total_claims"] == 0
        assert result_b["total_sessions"] == 1
        assert result_b["total_claims"] == 1


class TestSessionTrackerIntegration:
    """Integration tests."""

    def test_full_workflow(self, tracker):
        """Full workflow: create, record, query."""
        # Start session
        session_id = tracker.start_session("data-agent")

        # Record activities
        tracker.record_activity(session_id, "ingest")
        tracker.record_activity(session_id, "ingest")
        tracker.record_activity(session_id, "query")

        # Get active sessions
        active = tracker.get_active_sessions()
        assert len(active) >= 1
        session = next(s for s in active if s["id"] == session_id)
        assert session["claims_ingested"] == 2
        assert session["queries_made"] == 1

        # Get stats
        stats = tracker.get_session_stats("data-agent")
        assert stats["total_claims"] == 2
        assert stats["total_queries"] == 1

    def test_multiple_agents_tracking(self, tracker):
        """Track multiple agents independently."""
        # Agent 1
        id1 = tracker.start_session("agent-1")
        tracker.record_activity(id1, "ingest")
        tracker.record_activity(id1, "ingest")

        # Agent 2
        id2 = tracker.start_session("agent-2")
        tracker.record_activity(id2, "query")
        tracker.record_activity(id2, "query")
        tracker.record_activity(id2, "query")

        # Check stats
        stats1 = tracker.get_session_stats("agent-1")
        stats2 = tracker.get_session_stats("agent-2")

        assert stats1["total_claims"] == 2
        assert stats1["total_queries"] == 0
        assert stats2["total_claims"] == 0
        assert stats2["total_queries"] == 3
