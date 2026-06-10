from datetime import datetime

from memorymaster.stores._storage_shared import connect_ro, open_conn


def insert(db_path, record: dict) -> None:
    conn = open_conn(db_path)
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
    conn = connect_ro(db_path)
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
