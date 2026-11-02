"""T-2067: the latency report must tell a missing fallback from a fallback that failed or worked."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memorymaster.dreaming.ledger import DreamLedger

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure_consolidate_latency.py"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("measure_consolidate_latency", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(db, run_id, started, extractor, summary, latency_ms=0):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(db)) as conn, conn:  # temp fixture DB only
        conn.execute(
            "INSERT INTO dream_runs VALUES (?, 'completed', 0, ?, 'gemini-3.8-flash-low', ?, ?, ?, ?)",
            (run_id, extractor, started, started, started, summary if isinstance(summary, str) else json.dumps(summary)),
        )
        conn.execute(
            "INSERT INTO dream_provider_usage (run_id, provider, model, outcome, http_status, latency_ms,"
            " structured_valid, input_tokens, output_tokens, created_at)"
            " VALUES (?, 'antigravity', 'gemini-3.8-flash-low', 'timeout', 0, ?, 0, 0, 0, ?)",
            (run_id, latency_ms, started),
        )


@pytest.fixture
def database(tmp_path):
    db = tmp_path / "capture-control.db"
    DreamLedger(db)  # creates the schema in the temp file
    timeout = "TimeoutExpired"
    _run(db, "old-1", "2026-10-28T10:00:00+00:00", "haiku",
         {"consolidate_breaker": timeout, "consolidate_fallback": None}, 300000)
    _run(db, "old-2", "2026-10-29T10:00:00+00:00", "haiku",
         {"consolidate_breaker": timeout, "consolidate_fallback": None})
    _run(db, "post-1", "2026-11-02T02:20:00+00:00", "haiku",
         {"consolidate_breaker": timeout, "consolidate_fallback": "claude"})
    _run(db, "post-2", "2026-11-02T03:00:00+00:00", "haiku",
         {"consolidate_breaker": None, "consolidate_fallback": "claude"})
    _run(db, "post-3", "2026-11-02T04:00:00+00:00", "gemini-flash-lite-latest",
         {"consolidate_breaker": None, "consolidate_fallback": "google"})
    _run(db, "plain", "2026-11-02T05:00:00+00:00", "haiku", {"consolidate_breaker": None, "consolidate_fallback": None})
    _run(db, "broken", "2026-11-02T06:00:00+00:00", "haiku", "not json")
    return db


def test_default_window_splits_the_three_run_shapes_by_extractor(script, database):
    report = script.measure(database, now=NOW)

    assert report["window_days"] == 14
    assert report["runs_with_consolidate_breaker_TimeoutExpired"] == 3  # backward-compatible key
    assert report["invalid_run_summaries"] == 1
    assert report["calls"] == 7 and report["measured_calls"] == 1 and report["max_seconds"] == 300
    assert report["breaker_no_fallback"] == {"total": 2, "by_extractor_model": {"haiku": 2}}
    assert report["breaker_after_fallback"] == {"total": 1, "by_extractor_model": {"haiku": 1}}
    assert report["fallback_succeeded"] == {
        "total": 2, "by_extractor_model": {"haiku": 1, "gemini-flash-lite-latest": 1},
    }


def test_since_cuts_off_the_pre_fix_runs(script, database):
    report = script.measure(database, now=NOW, since=script.parse_since("2026-10-31T17:45:00Z"))

    assert report["since_utc"].startswith("2026-10-31T17:45:00")
    assert report["window_days"] == 2
    assert report["runs_with_consolidate_breaker_TimeoutExpired"] == 1
    assert report["breaker_no_fallback"] == {"total": 0, "by_extractor_model": {}}
    assert report["breaker_after_fallback"]["total"] == 1


def test_cli_since_flag_and_database_stays_read_only(script, database, capsys, monkeypatch):
    before = database.read_bytes()
    monkeypatch.setattr("sys.argv", ["measure", str(database), "--since", "2026-11-02T00:00:00"])

    script.main()

    out = json.loads(capsys.readouterr().out)
    assert out["since_utc"].startswith("2026-11-02T00:00:00") and out["breaker_after_fallback"]["total"] == 1
    assert database.read_bytes() == before
    missing = database.with_name("missing.db")
    with pytest.raises(Exception):  # noqa: B017 - sqlite3.OperationalError; mode=ro refuses a missing file
        script.measure(missing, now=NOW)
    assert not missing.exists()
