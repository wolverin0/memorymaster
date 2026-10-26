from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from memorymaster.operations import operational_review as review


NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


@pytest.fixture
def telemetry(tmp_path):
    state = tmp_path / "capture-control.db"
    claims = tmp_path / "claims.db"
    with sqlite3.connect(state) as connection:
        connection.executescript("""
            CREATE TABLE dream_captures(id INTEGER PRIMARY KEY, provider TEXT,
                session_hash TEXT, captured_at TEXT);
            CREATE TABLE dream_applications(capture_id INTEGER, created_claim_id INTEGER,
                action TEXT, applied_at TEXT);
        """)
    with sqlite3.connect(claims) as connection:
        connection.execute("CREATE TABLE claims(id INTEGER PRIMARY KEY, created_at TEXT)")
    return review.ReviewConfig(db=claims, capture_state_db=state,
                               hook_events_log=tmp_path / "hook-events.jsonl")


def capture(config, row_id, cli="codex", session="session", age=1):
    stamp = (NOW - timedelta(hours=age)).isoformat()
    with sqlite3.connect(config.capture_state_db) as connection:
        connection.execute("INSERT INTO dream_captures VALUES (?,?,?,?)",
                           (row_id, cli, session, stamp))


def event(cli="codex", count=1, age=0, **overrides):
    return {"ts": (NOW - timedelta(hours=age)).isoformat(), "cli": cli,
            "event": "recall_injected", "count": count, **overrides}


def log(path, *events):
    path.write_text("".join(json.dumps(value) + "\n" for value in events), encoding="utf-8")


def application(config, claim_id, *, capture_id=1, action="add", creation_age=.5):
    with sqlite3.connect(config.capture_state_db) as connection:
        connection.execute("INSERT INTO dream_applications VALUES (?,?,?,?)",
                           (capture_id, claim_id, action, NOW.isoformat()))
    if claim_id is not None:
        with sqlite3.connect(config.db) as connection:
            connection.execute("INSERT OR IGNORE INTO claims VALUES (?,?)",
                               (claim_id, (NOW - timedelta(hours=creation_age)).isoformat()))


def test_each_captured_cli_needs_its_own_recent_injections(telemetry):
    capture(telemetry, 1, "claude-code")
    capture(telemetry, 2, "codex")
    capture(telemetry, 3, "antigravity")
    capture(telemetry, 4, "opencode")
    capture(telemetry, 5, "codex")  # Another chunk is not another session.
    log(telemetry.hook_events_log, event("claude", 3), event("codex", 20, age=25),
        event("agy", 0), event("opencode", event="session_started"))

    result = review.check_hook_recall(telemetry, now=NOW)

    assert result.verdict is review.Verdict.WARN
    assert "zero_injections=agy,codex,opencode" in result.detail
    assert result.counts["claude_recall_injections_24h"] == 3
    assert result.counts["codex_captured_sessions_24h"] == 1


def test_rotation_and_utc_window_include_only_actual_recent_injections(telemetry):
    capture(telemetry, 1)
    log(telemetry.hook_events_log.with_suffix(".jsonl.1"), event(count=2, age=24))
    log(telemetry.hook_events_log, event(count=5, age=-1), event(count=10, age=25))
    before = telemetry.capture_state_db.read_bytes()

    result = review.check_hook_recall(telemetry, now=NOW)

    assert result.verdict is review.Verdict.PASS
    assert result.counts["codex_recall_injections_24h"] == 2
    assert telemetry.capture_state_db.read_bytes() == before


@pytest.mark.parametrize("bad", [None, [], event(count=True), event(count=-1),
                                  event(ts="invalid"), event(cli="/private/path")])
def test_invalid_telemetry_never_reports_green(telemetry, bad):
    capture(telemetry, 1)
    log(telemetry.hook_events_log, event(), bad)
    result = review.check_hook_recall(telemetry, now=NOW)
    assert result.verdict is review.Verdict.WARN
    assert result.counts["invalid_events"] == 1


def test_missing_telemetry_and_empty_windows_are_observable(telemetry, tmp_path):
    assert review.check_hook_recall(telemetry, now=NOW).verdict is review.Verdict.WARN
    assert review.check_capture_yield(telemetry, now=NOW).verdict is review.Verdict.WARN
    absent = tmp_path / "absent" / "state.db"
    config = review.ReviewConfig(db=telemetry.db, capture_state_db=absent)
    assert review.check_hook_recall(config, now=NOW).verdict is review.Verdict.WARN
    assert review.check_capture_yield(config, now=NOW).verdict is review.Verdict.WARN
    assert not absent.parent.exists()


def test_yield_counts_distinct_sessions_and_verified_new_add_outcomes(telemetry):
    capture(telemetry, 1)
    capture(telemetry, 2)  # Same session, another capture.
    capture(telemetry, 3, "claude", "other-session")
    capture(telemetry, 4, "agy", "old-session", age=169)
    application(telemetry, 11)
    application(telemetry, 11, capture_id=2)  # Retry does not create another memory.
    application(telemetry, 12, action="reinforce")
    application(telemetry, 13, creation_age=2)  # Existing before this capture.
    application(telemetry, 14, capture_id=4)
    application(telemetry, None, action="ignore")

    result = review.check_capture_yield(telemetry, now=NOW)

    assert result.verdict is review.Verdict.PASS
    assert result.counts == {"captured_sessions_7d": 2, "memories_created_7d": 1}
    assert "ratio=0.5000" in result.detail


def test_yield_fails_measured_twenty_memories_per_eleven_hundred_sessions(telemetry):
    with sqlite3.connect(telemetry.capture_state_db) as connection:
        connection.executemany("INSERT INTO dream_captures VALUES (?,?,?,?)", [
            (i, "codex", str(i), (NOW - timedelta(hours=1)).isoformat())
            for i in range(1, 1101)
        ])
    for i in range(20):
        application(telemetry, i + 1)
    result = review.check_capture_yield(telemetry, now=NOW)
    assert result.verdict is review.Verdict.FAIL
    assert result.counts == {"captured_sessions_7d": 1100, "memories_created_7d": 20}
    assert review.CAPTURE_YIELD_THRESHOLD_ENV in result.detail


def test_yield_threshold_is_explicit_and_invalid_override_fails(telemetry, monkeypatch):
    capture(telemetry, 1)
    assert review.check_capture_yield(telemetry, now=NOW).verdict is review.Verdict.FAIL
    monkeypatch.setenv(review.CAPTURE_YIELD_THRESHOLD_ENV, "0")
    assert review.check_capture_yield(telemetry, now=NOW).verdict is review.Verdict.PASS
    for value in ("nan", "inf", "-1", "broken"):
        monkeypatch.setenv(review.CAPTURE_YIELD_THRESHOLD_ENV, value)
        result = review.check_capture_yield(telemetry, now=NOW)
        assert result.verdict is review.Verdict.FAIL
        assert "invalid" in result.detail


def test_missing_creation_receipt_does_not_invent_success(telemetry):
    capture(telemetry, 1)
    with sqlite3.connect(telemetry.capture_state_db) as connection:
        connection.execute("INSERT INTO dream_applications VALUES (1,999,'add',?)",
                           (NOW.isoformat(),))
    assert review.check_capture_yield(telemetry, now=NOW).verdict is review.Verdict.WARN
