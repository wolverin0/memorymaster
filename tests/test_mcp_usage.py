from __future__ import annotations

import argparse
import csv
from datetime import datetime
import sqlite3

from memorymaster.surfaces.cli_handlers_basic import handle_mcp_usage_report
from memorymaster.surfaces.mcp_usage import insert, query_window


CREATE_USAGE_TABLE = """
CREATE TABLE IF NOT EXISTS mcp_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    latency_ms INTEGER,
    tenant_id TEXT,
    result_status TEXT NOT NULL
)
"""


def _db(tmp_path):
    db_path = tmp_path / "mcp_usage.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(CREATE_USAGE_TABLE)
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_insert_and_retrieve(tmp_path):
    db_path = _db(tmp_path)
    record = {
        "tool_name": "query_memory",
        "timestamp": "2026-05-10T12:00:00",
        "latency_ms": 42,
        "tenant_id": "tenant-a",
        "result_status": "ok",
    }

    insert(db_path, record)

    rows = query_window(db_path, datetime(2026, 5, 10, 0, 0, 0))
    assert rows == [record]


def test_csv_shape(tmp_path, capsys):
    db_path = _db(tmp_path)
    insert(db_path, {
        "tool_name": "ingest_claim",
        "timestamp": "2026-05-10T12:00:00",
        "latency_ms": 7,
        "tenant_id": None,
        "result_status": "ok",
    })
    args = argparse.Namespace(since="2026-05-10T00:00:00", format="csv")

    assert handle_mcp_usage_report(args, db_path) == 0

    captured = capsys.readouterr()
    rows = list(csv.reader(captured.out.splitlines()))
    assert rows[0] == ["tool_name", "timestamp", "latency_ms", "tenant_id", "result_status"]
    assert rows[1] == ["ingest_claim", "2026-05-10T12:00:00", "7", "", "ok"]
    assert len(rows) == 2


def test_window_filter(tmp_path):
    db_path = _db(tmp_path)
    insert(db_path, {
        "tool_name": "old_tool",
        "timestamp": "2026-05-01T12:00:00",
        "latency_ms": 100,
        "tenant_id": "tenant-a",
        "result_status": "ok",
    })
    insert(db_path, {
        "tool_name": "recent_tool",
        "timestamp": "2026-05-10T12:00:00",
        "latency_ms": 10,
        "tenant_id": "tenant-a",
        "result_status": "error",
    })

    rows = query_window(db_path, datetime(2026, 5, 5, 0, 0, 0))
    assert [row["tool_name"] for row in rows] == ["recent_tool"]


def test_empty_window(tmp_path):
    db_path = _db(tmp_path)

    assert query_window(db_path, datetime(2026, 5, 1, 0, 0, 0)) == []
