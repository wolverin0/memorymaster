"""AO-2 v1 beat: read-only health check with M3 lag and M4 guard summary (T-1640 AC4)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.operations import operational_review as review

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _config(tmp_path: Path, **kwargs) -> review.ReviewConfig:
    return review.ReviewConfig(
        db=tmp_path / "memory.db",
        capture_state_db=tmp_path / "state" / "capture-control.db",
        beat_guard_log=tmp_path / "state" / "beat-guard.jsonl",
        **kwargs,
    )


def _capture(ledger: DreamLedger, n: int, *, provider: str, lag_s: float, state: str = "extracted",
             processed_ago_s: float = 600) -> None:
    """Insert a capture whose source file was last written `lag_s` before it was processed."""
    processed = NOW - timedelta(seconds=processed_ago_s)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            """INSERT INTO dream_captures (capture_key, version, provider, session_hash, scope, captured_at,
               last_activity_at, messages_json, cursor_start, cursor_end, content_hash, byte_count,
               turn_count, state, updated_at, source_mtime)
               VALUES (?, 'v', ?, 's', 'project:x', ?, ?, '[]', 0, 1, 'h', 1, 2, ?, ?, ?)""",
            (f"{provider}-{n}-{state}", provider, (processed - timedelta(seconds=30)).isoformat(),
             processed.isoformat(), state, processed.isoformat(), processed.timestamp() - lag_s),
        )


def _guard(tmp_path: Path, actions: list[str]) -> None:
    path = tmp_path / "state" / "beat-guard.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for index, action in enumerate(actions):
            stamp = (NOW - timedelta(minutes=5 * (index + 1))).isoformat(timespec="seconds")
            handle.write(json.dumps({"ts": stamp, "action": action, "reasons": []}) + "\n")


def test_the_check_is_registered_and_takes_only_the_review_config() -> None:
    assert review.check_beat_health in review.REVIEW_CHECKS


def test_no_state_and_no_guard_log_is_a_pass_not_a_warning(tmp_path: Path) -> None:
    result = review.check_beat_health(_config(tmp_path), now=NOW)

    assert result.verdict is review.Verdict.PASS
    assert "no beat data" in result.detail
    assert not (tmp_path / "state").exists()  # read-only: nothing was created


def test_lag_percentiles_are_reported_per_provider(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "state" / "capture-control.db")
    for index, lag in enumerate([100, 200, 300, 400, 500]):
        _capture(ledger, index, provider="claude", lag_s=lag)
    _capture(ledger, 9, provider="codex", lag_s=60)

    result = review.check_beat_health(_config(tmp_path), now=NOW)

    assert result.verdict is review.Verdict.PASS
    assert result.counts["claude_lag_p50_s"] == 300
    assert result.counts["claude_lag_p95_s"] == 500
    assert result.counts["codex_lag_p50_s"] == 60
    assert result.counts["processed_24h"] == 6


def test_lag_over_an_hour_warns(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "state" / "capture-control.db")
    for index in range(5):
        _capture(ledger, index, provider="claude", lag_s=5000)

    result = review.check_beat_health(_config(tmp_path), now=NOW)

    assert result.verdict is review.Verdict.WARN
    assert "lag" in result.detail


def _hook_capture(ledger: DreamLedger, key: str, *, state: str, ago_s: float = 600, turns: int = 2) -> None:
    """A hook-captured row: it never gets a source mtime, so it carries no lag figure."""
    stamp = (NOW - timedelta(seconds=ago_s)).isoformat()
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            """INSERT INTO dream_captures (capture_key, version, provider, session_hash, scope, captured_at,
               last_activity_at, messages_json, cursor_start, cursor_end, content_hash, byte_count,
               turn_count, state, updated_at) VALUES (?,'v','claude','s','p',?,?,'[]',0,1,'h',1,?,?,?)""",
            (key, stamp, stamp, turns, state, stamp),
        )


def test_unprocessed_rows_do_not_enter_the_lag_percentiles(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "state" / "capture-control.db")
    _capture(ledger, 1, provider="claude", lag_s=500, state="extracted")
    _capture(ledger, 2, provider="claude", lag_s=90000, state="captured")  # still waiting
    _hook_capture(ledger, "hook-applied", state="applied")  # processed, but no mtime to measure

    result = review.check_beat_health(_config(tmp_path), now=NOW)

    assert result.counts["claude_lag_p95_s"] == 500


def test_processed_counts_every_capture_even_when_the_beat_scan_only_queues_others(tmp_path: Path) -> None:
    """Live 2026-10-31: the beat extracted ~70 hook captures a day (no source mtime) while the 768
    beat-scanned ones waited; the review reported processed_24h=0 and no lag at all."""
    ledger = DreamLedger(tmp_path / "state" / "capture-control.db")
    for index in range(3):
        _hook_capture(ledger, f"hook-{index}", state="applied")
    _capture(ledger, 1, provider="codex", lag_s=100000, state="captured", processed_ago_s=100000)

    result = review.check_beat_health(_config(tmp_path), now=NOW)

    assert result.counts["processed_24h"] == 3
    assert result.counts["processed_scanned_24h"] == 0
    assert result.counts["pending_scanned"] == 1
    assert "claude_lag_p95_s" not in result.counts  # nothing scanned was processed: no percentile to invent


def test_a_scanned_capture_waiting_past_the_target_warns_even_with_nothing_processed(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "state" / "capture-control.db")
    _capture(ledger, 1, provider="codex", lag_s=0, state="captured", processed_ago_s=7200)

    result = review.check_beat_health(_config(tmp_path), now=NOW)

    assert result.verdict is review.Verdict.WARN
    assert result.counts["pending_scanned_oldest_age_s"] == 7200
    assert "pending" in result.detail


def test_a_fresh_scanned_capture_waiting_is_not_a_warning(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "state" / "capture-control.db")
    _capture(ledger, 1, provider="codex", lag_s=0, state="captured", processed_ago_s=600)

    assert review.check_beat_health(_config(tmp_path), now=NOW).verdict is review.Verdict.PASS


def test_pending_counts_every_eligible_capture_and_skips_single_turn_rows(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "state" / "capture-control.db")
    _capture(ledger, 1, provider="codex", lag_s=10, state="captured")
    _hook_capture(ledger, "hook-waiting", state="captured")
    _hook_capture(ledger, "hook-one-turn", state="captured", turns=1)  # never eligible for extraction

    result = review.check_beat_health(_config(tmp_path), now=NOW)

    assert result.counts["pending"] == 2 and result.counts["pending_scanned"] == 1


def test_hook_captures_alone_leave_the_beat_inactive(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "state" / "capture-control.db")
    _hook_capture(ledger, "hook-applied", state="applied")
    _hook_capture(ledger, "hook-waiting", state="captured")

    result = review.check_beat_health(_config(tmp_path), now=NOW)

    assert result.verdict is review.Verdict.PASS and "beat inactive" in result.detail


def test_m4_from_the_guard_log(tmp_path: Path) -> None:
    _guard(tmp_path, ["allow"] * 20)
    assert review.check_beat_health(_config(tmp_path), now=NOW).verdict is review.Verdict.PASS

    _guard(tmp_path, ["capture_only"] * 2)
    warn = review.check_beat_health(_config(tmp_path), now=NOW)
    assert warn.verdict is review.Verdict.WARN and warn.counts["guard_capture_only"] == 2

    _guard(tmp_path, ["skip"] * 6)
    fail = review.check_beat_health(_config(tmp_path), now=NOW)
    assert fail.verdict is review.Verdict.FAIL and fail.counts["guard_skip"] == 6


def test_old_guard_events_are_outside_the_window(tmp_path: Path) -> None:
    path = tmp_path / "state" / "beat-guard.jsonl"
    path.parent.mkdir(parents=True)
    old = (NOW - timedelta(days=3)).isoformat(timespec="seconds")
    path.write_text("".join(json.dumps({"ts": old, "action": "skip", "reasons": []}) + "\n" for _ in range(10)),
                    encoding="utf-8")

    assert review.check_beat_health(_config(tmp_path), now=NOW).verdict is review.Verdict.PASS


def test_the_check_never_writes_a_measurement_artifact(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _guard(tmp_path, ["allow"])

    review.check_beat_health(_config(tmp_path), now=NOW)

    assert not (tmp_path / "artifacts").exists()


def test_default_paths_follow_the_state_db_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(tmp_path / "state" / "capture-control.db"))
    _guard(tmp_path, ["skip"] * 6)

    result = review.check_beat_health(review.ReviewConfig(db=tmp_path / "memory.db"), now=NOW)

    assert result.verdict is review.Verdict.FAIL
