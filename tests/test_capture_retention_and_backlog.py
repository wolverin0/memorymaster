from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.govern.review import build_candidate_backlog_plan
from memorymaster.govern.verbatim_cleanup import plan_retention


def _verbatim_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE verbatim_memories (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.executemany(
        "INSERT INTO verbatim_memories VALUES (?, ?, ?, ?, ?)",
        [
            (1, "old", "x" * 40, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            (2, "recent-a", "y" * 40, "2026-07-12T00:00:00+00:00", "2026-07-12T00:00:00+00:00"),
            (3, "recent-b", "z" * 40, "2026-07-13T00:00:00+00:00", "2026-07-13T00:00:00+00:00"),
        ],
    )
    conn.commit()
    conn.close()


def test_retention_plan_enforces_age_bytes_sessions_without_mutation(tmp_path: Path) -> None:
    db = tmp_path / "verbatim.db"
    _verbatim_db(db)
    before = db.read_bytes()

    plan = plan_retention(
        str(db),
        max_age_days=30,
        max_bytes=50,
        max_sessions=1,
        now=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )

    assert plan["dry_run"] is True
    assert plan["limits"] == {"max_age_days": 30, "max_bytes": 50, "max_sessions": 1}
    assert plan["candidate_rows"] == 2
    assert plan["retained_rows"] == 1
    assert plan["retained_bytes"] <= 50
    assert db.read_bytes() == before


@dataclass
class _Claim:
    id: int
    status: str = "candidate"


class _ReadOnlyService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def list_claims(self, **kwargs):
        self.calls.append(kwargs)
        return [_Claim(index) for index in range(1, 1201)]


def test_backlog_plan_is_bounded_reviewable_and_read_only() -> None:
    service = _ReadOnlyService()
    plan = build_candidate_backlog_plan(service, daily_capacity=688, batch_size=100, scan_limit=5000)

    assert plan["dry_run"] is True
    assert plan["candidate_count"] == 1200
    assert plan["review_batches"] == 12
    assert plan["minimum_days"] == 2
    assert plan["automatic_transitions"] == 0
    assert service.calls == [
        {
            "status": "candidate",
            "include_archived": False,
            "limit": 5000,
            "allow_sensitive": False,
        }
    ]
