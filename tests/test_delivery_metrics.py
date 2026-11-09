"""T-2272: the delivery metrics report is read-only and its maths are pinned on tmp fixtures."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from memorymaster.dreaming.ledger import DreamLedger
from scripts import delivery_metrics as dm

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=7)


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


@pytest.mark.parametrize("text", [
    "You have 1 orchestration message waiting",
    "You have 12 orchestration messages",
    "\nYou have 1 orchestration message",
    "  [ficha-ping 3]",
    "<task-notification>done</task-notification>",
    '<subagent sessionID="ses_1">',
    "[SYSTEM NOTIFICATION] disk",
])
def test_machine_notices_are_classified(text):
    assert dm.classify_query(text)


@pytest.mark.parametrize("text", [
    "Dame las win de las últimas 12 horas",
    "Check PowerBeam PtP OpenWrt BQL",
    "what does the <subagent> tag mean?",  # pattern must anchor at the start
    "",
])
def test_human_prompts_are_not_notices(text):
    assert not dm.classify_query(text)


def test_percentile_is_nearest_rank():
    assert dm.percentile([], 0.95) is None
    assert dm.percentile([7], 0.95) == 7
    values = list(range(1, 21))  # n=20: nearest rank 19; int(0.95*20) would give index 19 -> 20
    assert dm.percentile(values, 0.95) == 19
    assert dm.percentile(values, 0.50) == 10


def test_open_ro_refuses_writes_and_missing_files(tmp_path):
    db = tmp_path / "a.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x)")
    ro = dm.open_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (1)")
    ro.close()
    with pytest.raises(sqlite3.OperationalError):
        dm.open_ro(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def make_claims_db(path, rows):
    with sqlite3.connect(path) as conn:  # exact DDL of govern/feedback.py
        conn.executescript("""
            CREATE TABLE usage_feedback (id TEXT PRIMARY KEY, claim_id INTEGER NOT NULL,
                query_text TEXT NOT NULL, timestamp TEXT NOT NULL,
                was_returned INTEGER NOT NULL DEFAULT 1, score REAL);
            CREATE INDEX idx_uf_timestamp ON usage_feedback(timestamp);""")
        conn.executemany("INSERT INTO usage_feedback VALUES (?,?,?,?,?,NULL)", [
            (f"id{i}", claim, query, ts, returned) for i, (claim, query, ts, returned) in enumerate(rows)])


def test_machine_notice_share_counts_returns_and_events(tmp_path):
    db = tmp_path / "m.db"
    t = iso(1)
    make_claims_db(db, [
        (1, "\nYou have 1 orchestration message", t, 1),
        (2, "\nYou have 1 orchestration message", t, 1),  # same event, second claim
        (3, "Dame las win de hoy", t, 1),
        (4, "Dame las win de hoy", iso(2), 1),
        (5, "You have 1 orchestration message", iso(24 * 30), 1),  # outside the window
        (6, "You have 1 orchestration message", t, 0),  # not returned
        (7, "Extract at most five candidates", t, 1),
    ])
    conn = dm.open_ro(db)
    result = dm.machine_notice_share(conn, SINCE)
    conn.close()
    assert result["recall_returns"] == 5
    assert result["machine_notice_returns"] == 2
    assert result["share_of_returns"] == 0.4
    assert result["recall_events"] == 4 and result["machine_notice_events"] == 1
    prefixes = {p["prefix"]: p for p in result["top_unmatched_prefixes"]}
    assert prefixes["Dame las win de hoy"]["returns"] == 2
    assert "Extract at most five candid" in "".join(prefixes)


def test_dream_queue_and_p95_hours(tmp_path):
    db = tmp_path / "ledger.db"
    ledger = DreamLedger(db)
    with sqlite3.connect(db) as conn:
        columns = "capture_key,version,provider,session_hash,scope,captured_at,last_activity_at,messages_json," \
                  "cursor_start,cursor_end,content_hash,byte_count,turn_count,state,updated_at"
        for n in range(20):  # captured n+1 hours before being applied at NOW-1h
            conn.execute(f"INSERT INTO dream_captures (id,{columns}) VALUES (?,?,'v','p','s','scope',?,?,'[]',0,0,'h',0,0,'applied',?)",
                         (n + 1, f"k{n}", iso(n + 2), iso(n + 2), iso(1)))
            conn.execute("INSERT INTO dream_applications VALUES (?,?,?,?,?,NULL,NULL,?)",
                         (f"a{n}", "r", n + 1, "c", "ignore", iso(1)))
        conn.execute(f"INSERT INTO dream_captures (id,{columns}) VALUES (99,'kq','v','p','s','scope',?,?,'[]',0,0,'h',0,0,'captured',?)",
                     (iso(10), iso(10), iso(10)))
    del ledger
    conn = dm.open_ro(db)
    result = dm.dream_queue(conn, SINCE, NOW)
    conn.close()
    assert result["queue_by_status"] == {"applied": 20, "captured": 1}
    assert result["oldest_captured_age_hours"] == 10.0
    assert result["captures_applied_in_window"] == 20
    assert result["capture_to_applied_hours_p95"] == 19.0  # hours 1..20, nearest rank 19
    assert result["capture_to_applied_hours_p50"] == 10.0


def test_hook_telemetry_reports_null_with_reason_when_absent(tmp_path):
    result = dm.hook_telemetry(tmp_path / "none.jsonl", SINCE)
    assert result["injected_tokens_per_turn"]["value"] is None
    assert "not found" in result["hook_latency"]["reason"]


def test_hook_telemetry_percentiles(tmp_path):
    log = tmp_path / "recall-dense.jsonl"
    events = [{"ts": iso(1), "chars": 400 * n, "total_ms": 100.0 * n} for n in range(0, 21)]
    log.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    result = dm.hook_telemetry(log, SINCE)
    tokens = result["injected_tokens_per_turn"]
    assert tokens["zero_injection_events"] == 1 and tokens["injecting_events"] == 20
    assert tokens["p95"] == 1900  # chars 400*19 // 4
    assert result["hook_latency"]["p95_ms"] == 1900.0  # 21 events, rank ceil(19.95)=20 -> n=19


def test_collect_end_to_end_is_read_only(tmp_path):
    db, ledger_db = tmp_path / "m.db", tmp_path / "ledger.db"
    make_claims_db(db, [(1, "<task-notification>x", iso(1), 1)])
    DreamLedger(ledger_db)
    before = (db.stat().st_mtime_ns, ledger_db.stat().st_mtime_ns)
    report = dm.collect(db, ledger_db, tmp_path, tmp_path / "recall-dense.jsonl", 7, NOW)
    assert (db.stat().st_mtime_ns, ledger_db.stat().st_mtime_ns) == before
    assert report["machine_notice_recall_share"]["share_of_returns"] == 1.0
    assert report["injected_claims_cited"]["injected_claims"] == 0
    assert report["hook_latency"]["value"] is None
    json.dumps(report)


def test_missing_database_becomes_null_section(tmp_path):
    report = dm.collect(tmp_path / "no.db", tmp_path / "no2.db", tmp_path, tmp_path / "x.jsonl", 7, NOW)
    assert report["machine_notice_recall_share"]["value"] is None
    assert report["dreaming"]["value"] is None
