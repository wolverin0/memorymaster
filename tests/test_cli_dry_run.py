from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.cli import main
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "dry_run.db"
    assert main(["--db", str(db), "init-db"]) == 0
    return db


def _run(capsys, argv: list[str]) -> tuple[int, str]:
    capsys.readouterr()
    rc = main(argv)
    out = capsys.readouterr().out
    return rc, out


def _ingest(
    db: Path,
    text: str,
    *,
    source: str = "dry-run-test",
    volatility: str = "medium",
    confidence: str = "0.5",
) -> int:
    assert main([
        "--db",
        str(db),
        "ingest",
        "--text",
        text,
        "--source",
        source,
        "--volatility",
        volatility,
        "--confidence",
        confidence,
    ]) == 0
    with sqlite3.connect(db) as conn:
        return int(conn.execute("SELECT max(id) FROM claims").fetchone()[0])


def _old_iso(days: int = 90) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _claim_state(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT id, status, confidence, updated_at, archived_at FROM claims ORDER BY id"
        ).fetchall()


def _event_count(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute("SELECT count(*) FROM events").fetchone()[0])


def _set_claim(db: Path, claim_id: int, **fields: object) -> None:
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with sqlite3.connect(db) as conn:
        conn.execute(f"UPDATE claims SET {assignments} WHERE id = ?", [*fields.values(), claim_id])


def test_compact_dry_run_no_writes(tmp_db: Path, tmp_path: Path, capsys) -> None:
    claim_id = _ingest(tmp_db, "compact dry-run candidate")
    _set_claim(tmp_db, claim_id, status="stale", updated_at=_old_iso())
    before_claims = _claim_state(tmp_db)
    before_events = _event_count(tmp_db)
    workspace = tmp_path / "workspace"

    rc, out = _run(capsys, [
        "--db",
        str(tmp_db),
        "--workspace",
        str(workspace),
        "compact",
        "--dry-run",
    ])

    assert rc == 0
    assert _claim_state(tmp_db) == before_claims
    assert _event_count(tmp_db) == before_events
    assert not (workspace / "artifacts" / "compaction" / "summary_graph.json").exists()
    assert f"claim={claim_id} stale -> archived" in out
    assert "summary_graph.json" in out
    assert "traceability.json" in out


def test_dedup_dry_run_no_writes(tmp_db: Path, capsys) -> None:
    service = MemoryService(tmp_db)
    service.ingest(
        "dedup dry-run duplicate",
        citations=[CitationInput(source="dedup-a")],
        idempotency_key="dedup-a",
    )
    service.ingest(
        "dedup dry-run duplicate",
        citations=[CitationInput(source="dedup-b")],
        idempotency_key="dedup-b",
    )
    before_claims = _claim_state(tmp_db)
    before_events = _event_count(tmp_db)

    rc, out = _run(capsys, ["--db", str(tmp_db), "dedup", "--dry-run"])

    assert rc == 0
    assert _claim_state(tmp_db) == before_claims
    assert _event_count(tmp_db) == before_events
    assert "dedup [DRY RUN]" in out
    assert "dup: keep=" in out
    assert "archive=" in out


def test_decay_dry_run_no_writes(tmp_db: Path, capsys) -> None:
    claim_id = _ingest(
        tmp_db,
        "decay dry-run stale transition",
        volatility="high",
        confidence="0.9",
    )
    _set_claim(tmp_db, claim_id, status="confirmed", confidence=0.9, updated_at=_old_iso(30))
    before_claims = _claim_state(tmp_db)
    before_events = _event_count(tmp_db)

    rc, out = _run(capsys, ["--db", str(tmp_db), "decay", "--dry-run"])

    assert rc == 0
    assert _claim_state(tmp_db) == before_claims
    assert _event_count(tmp_db) == before_events
    assert "decay [DRY RUN]" in out
    assert f"claim={claim_id} confirmed -> stale" in out
