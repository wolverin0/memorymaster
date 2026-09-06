"""Content-free, deterministic source sampling on a read-only SQLite snapshot."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memorymaster.dreaming.sampling import sample_sources


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE dream_captures (
                id INTEGER PRIMARY KEY, content_hash TEXT, session_hash TEXT,
                captured_at TEXT, state TEXT, turn_count INTEGER, extraction_json TEXT);
            CREATE TABLE dream_applications (capture_id INTEGER, created_claim_id INTEGER);
        """)
        for i, extracted in enumerate([None, "bad", "[]", '[{"text":"PRIVATE-SOURCE"}]',
                                       '[{"text":"PRIVATE-SOURCE"}]'], 1):
            conn.execute("INSERT INTO dream_captures VALUES (?,?,?,?,?,?,?)",
                         (i, str(i), "session", "2026-09-01T10:00:00Z", "applied", 3, extracted))
        conn.execute("INSERT INTO dream_applications VALUES (5,100)")
    return path


def test_all_source_strata_are_visible_without_source_text(ledger):
    before = ledger.read_bytes()
    report = sample_sources(ledger, since="2026-09-01T00:00:00Z",
                            until="2026-09-02T00:00:00Z", per_stratum=1)
    assert report["population"] == 5
    assert set(report["counts"]) == {
        "unprocessed", "malformed", "zero_candidates",
        "candidates_no_actions", "candidates_with_actions",
    }
    assert len(report["sample"]) == 5
    assert "PRIVATE-SOURCE" not in json.dumps(report)
    assert report["semantic_precision"] is None
    assert ledger.read_bytes() == before
    assert report == sample_sources(ledger, since="2026-09-01T00:00:00Z",
                                   until="2026-09-02T00:00:00Z", per_stratum=1)


def test_empty_window_is_not_quality_success(ledger):
    report = sample_sources(ledger, since="2026-09-02T00:00:00Z",
                            until="2026-09-03T00:00:00Z")
    assert report["population"] == 0
    assert report["sample"] == []
    assert report["semantic_precision"] is None


@pytest.mark.parametrize("kwargs", [{"per_stratum": 0}, {"per_stratum": 101},
                                    {"since": "not-a-date"},
                                    {"until": "2026-08-01T00:00:00Z"}])
def test_invalid_window_or_bounds_fail_closed(ledger, kwargs):
    options = {"since": "2026-09-01T00:00:00Z", "until": "2026-09-02T00:00:00Z"}
    options.update(kwargs)
    with pytest.raises(ValueError):
        sample_sources(ledger, **options)


def test_missing_ledger_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        sample_sources(path, since="2026-09-01T00:00:00Z", until="2026-09-02T00:00:00Z")
    assert not path.exists()


def test_cli_cannot_overwrite_source_ledger(ledger):
    before = ledger.read_bytes()
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/sample_dreaming.py"),
         "--ledger", str(ledger), "--since", "2026-09-01T00:00:00Z",
         "--until", "2026-09-02T00:00:00Z", "--out", str(ledger)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "never the source ledger" in result.stderr
    assert ledger.read_bytes() == before
