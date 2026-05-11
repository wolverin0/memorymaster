from datetime import datetime
from pathlib import Path
import sqlite3


def insert(db_path, record: dict) -> None:
    conn = sqlite3.connect(Path(db_path))
    try:
        conn.execute(
            """
            INSERT INTO mcp_usage (
                tool_name, timestamp, latency_ms, tenant_id, result_status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                record["tool_name"],
                record["timestamp"],
                record.get("latency_ms"),
                record.get("tenant_id"),
                record["result_status"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def query_window(db_path, since_dt: datetime) -> list[dict]:
    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT tool_name, timestamp, latency_ms, tenant_id, result_status
            FROM mcp_usage
            WHERE timestamp >= ?
            ORDER BY timestamp ASC, id ASC
            """,
            (since_dt.isoformat(),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
