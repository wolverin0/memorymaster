"""Historical burndown reads disposable auxiliary state without touching live M3."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from memorymaster.operations import operational_review as review


def _config(tmp_path: Path) -> review.ReviewConfig:
    return review.ReviewConfig(db=tmp_path / "never-open-authority.db",
                               capture_state_db=tmp_path / "capture-state.db")


def _inventory(tmp_path: Path) -> Path:
    path = tmp_path / "dream_backfill_ledger.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE backfill_inventory (
                campaign_id TEXT, stream_id TEXT, provider TEXT, parent_session_id TEXT,
                scope TEXT, status TEXT, frozen_end INTEGER, read_offset INTEGER,
                source_locator TEXT, metadata_json TEXT, cohort_id TEXT
            );
            CREATE TABLE backfill_reservations (
                campaign_id TEXT, stage TEXT, input_tokens INTEGER, output_tokens INTEGER
            );
        """)
        conn.executemany("INSERT INTO backfill_inventory VALUES (?,?,?,?,?,?,?,?,?,?,'fixture-0')", [
            ("ao7", "main", "claude", "one", "project:test", "applied", 100, 100,
             "PRIVATE_SOURCE_PATH", '{"text":"PRIVATE_TRANSCRIPT"}'),
            ("ao7", "child", "claude", "one", "project:test", "complete_sampled", 50, 50,
             "PRIVATE_SOURCE_PATH", "{}"),
            ("ao7", "next", "codex", "two", "project:other", "selected", 200, 100,
             "PRIVATE_SOURCE_PATH", "{}"),
            ("ao7", "unknown", "agy", "three", None, "unresolved_scope", 40, 0,
             "PRIVATE_SOURCE_PATH", "{}"),
        ])
        conn.execute("INSERT INTO backfill_reservations VALUES ('ao7','extract',10000,1500)")
    return path


def test_absent_backfill_is_inactive_without_creating_state(tmp_path):
    before = set(tmp_path.iterdir())
    result = review.check_backfill_burndown(_config(tmp_path))
    assert result.verdict is review.Verdict.PASS
    assert "inactive" in result.detail
    assert set(tmp_path.iterdir()) == before


def test_backfill_counts_independent_sessions_and_reports_safe_dimensions(tmp_path, monkeypatch):
    from memorymaster.dreaming import backfill

    ledger = _inventory(tmp_path)
    before = ledger.read_bytes()
    connections = []
    native_connect = sqlite3.connect

    def read_only_connect(database, *args, **kwargs):
        assert kwargs.get("uri") is True and "mode=ro" in str(database)
        connections.append(str(database))
        return native_connect(database, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Writable ledger construction is forbidden")

    monkeypatch.setattr(sqlite3, "connect", read_only_connect)
    monkeypatch.setattr(backfill, "DreamLedger", forbidden)
    result = review.check_backfill_burndown(_config(tmp_path))
    assert result.verdict is review.Verdict.PASS
    assert result.counts["files"] == 4
    assert result.counts["sessions"] == 2
    assert result.counts["completed_sessions"] == 1
    assert result.counts["remaining_sessions"] == 1
    assert result.counts["progress_basis_points"] == 5000
    assert result.counts["sources:claude:sessions"] == 1
    assert result.counts["sources:claude:completed_sessions"] == 1
    assert result.counts["projects:project:other:remaining_sessions"] == 1
    assert result.counts["projects:unresolved:files"] == 1
    assert result.counts["disposition:unresolved_scope"] == 1
    assert result.counts["usage:extract:input_tokens"] == 10000
    assert connections and ledger.read_bytes() == before
    assert not _config(tmp_path).db.exists()
    payload = json.dumps(asdict(result))
    assert "PRIVATE_" not in payload and str(tmp_path) not in payload
    assert "M3" not in result.detail


def test_unreadable_backfill_warns_without_disclosing_content_or_paths(tmp_path):
    (tmp_path / "dream_backfill_ledger.db").write_text("PRIVATE_TRANSCRIPT", encoding="utf-8")
    result = review.check_backfill_burndown(_config(tmp_path))
    assert result.verdict is review.Verdict.WARN
    assert result.detail == "probe_error=DatabaseError"
    assert "PRIVATE_" not in json.dumps(asdict(result))
    assert str(tmp_path) not in result.detail


def test_default_state_path_can_be_resolved_without_reading_home(tmp_path, monkeypatch):
    from memorymaster.core import capture_control

    monkeypatch.setattr(capture_control, "capture_state_path", lambda: tmp_path / "capture-state.db")
    result = review.check_backfill_burndown(review.ReviewConfig(db=tmp_path / "unused.db"))
    assert result.verdict is review.Verdict.PASS
    assert "inactive" in result.detail
    assert not list(tmp_path.iterdir())


def test_backfill_has_a_separate_registered_check():
    assert review.check_beat_health in review.REVIEW_CHECKS
    assert review.check_backfill_burndown in review.REVIEW_CHECKS
    assert review.REVIEW_CHECKS.count(review.check_backfill_burndown) == 1


def test_unknown_labels_cannot_disclose_source_locators(tmp_path, monkeypatch):
    from memorymaster.dreaming import backfill

    monkeypatch.setattr(backfill, "read_backfill_status", lambda state_dir: {
        "state": "inventoried", "files": 1, "sessions": 1, "completed_sessions": 0,
        "sources": {"PRIVATE_SOURCE_PATH": {"files": 1}},
        "projects": {"project:C:/PRIVATE_SOURCE_PATH": {"files": 1}},
        "dispositions": {"PRIVATE_TRANSCRIPT": 1},
        "usage_reservations": [{"stage": "PRIVATE_TRANSCRIPT", "calls": 1}],
    })
    result = review.check_backfill_burndown(_config(tmp_path))
    assert result.verdict is review.Verdict.PASS
    assert "PRIVATE_" not in json.dumps(asdict(result))
