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


def test_unprocessed_and_pre_beat_rows_do_not_enter_the_lag_numbers(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "state" / "capture-control.db")
    _capture(ledger, 1, provider="claude", lag_s=90000, state="captured")  # still waiting
    with sqlite3.connect(ledger.db_path) as conn:  # hook-captured: no source mtime
        conn.execute(
            """INSERT INTO dream_captures (capture_key, version, provider, session_hash, scope, captured_at,
               last_activity_at, messages_json, cursor_start, cursor_end, content_hash, byte_count,
               turn_count, state, updated_at) VALUES ('k','v','claude','s','p',?,?,'[]',0,1,'h',1,2,'applied',?)""",
            (NOW.isoformat(),) * 3,
        )

    result = review.check_beat_health(_config(tmp_path), now=NOW)

    assert result.verdict is review.Verdict.PASS
    assert result.counts["processed_24h"] == 0 and result.counts["pending"] == 1


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
