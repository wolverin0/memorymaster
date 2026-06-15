from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.stores import snapshot


CLAIM_STATUSES = (
    "candidate",
    "confirmed",
    "stale",
    "conflicted",
    "archived",
    "superseded",
    "candidate",
    "confirmed",
    "stale",
    "conflicted",
)


def _init_db(path: Path, workspace: Path) -> MemoryService:
    service = MemoryService(path, workspace_root=workspace)
    service.init_db()
    return service


def _seed_claims(db_path: Path, workspace: Path) -> None:
    service = _init_db(db_path, workspace)
    for idx, status in enumerate(CLAIM_STATUSES, start=1):
        citations = [
            CitationInput(
                source=f"session://roundtrip/{idx}",
                locator=f"turn-{idx}",
                excerpt=f"evidence {idx}",
            )
        ]
        if idx % 2 == 0:
            citations.append(
                CitationInput(
                    source=f"file://fixture/{idx}.md",
                    locator=f"L{idx}",
                    excerpt=f"secondary evidence {idx}",
                )
            )
        service.ingest(
            text=f"Round-trip claim {idx}",
            citations=citations,
            claim_type=("fact" if idx % 3 else "decision"),
            subject=f"subject-{idx}",
            predicate=f"predicate-{idx % 4}",
            object_value=f"object-{idx}",
            scope=f"project:roundtrip:{idx % 3}",
            volatility=("low" if idx % 2 else "high"),
            confidence=idx / 10,
            event_time=f"2026-01-{idx:02d}T00:00:00+00:00",
            valid_from=f"2026-02-{idx:02d}T00:00:00+00:00",
            valid_until=(f"2026-03-{idx:02d}T00:00:00+00:00" if idx % 4 == 0 else None),
            source_agent="pytest-roundtrip",
        )

    with sqlite3.connect(str(db_path)) as conn:
        for idx, status in enumerate(CLAIM_STATUSES, start=1):
            conn.execute(
                """
                UPDATE claims
                SET status = ?,
                    tier = ?,
                    pinned = ?,
                    last_validated_at = ?,
                    archived_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    "episodic" if idx % 2 else "semantic",
                    idx % 2,
                    f"2026-04-{idx:02d}T00:00:00+00:00",
                    f"2026-05-{idx:02d}T00:00:00+00:00" if status == "archived" else None,
                    f"2026-06-{idx:02d}T00:00:00+00:00",
                    idx,
                ),
            )
        conn.execute(
            """
            INSERT INTO events (
                claim_id, event_type, from_status, to_status, details, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                None,
                "system",
                None,
                None,
                "roundtrip sentinel",
                '{"source":"test"}',
                "2026-07-01T00:00:00+00:00",
            ),
        )
        conn.commit()


def _table_rows(db_path: Path, table: str) -> list[dict[str, object]]:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
        selected = ", ".join(columns)
        return [
            dict(row)
            for row in conn.execute(f"SELECT {selected} FROM {table} ORDER BY id")
        ]


def _roundtrip(source_db: Path, restored_db: Path, backup_file: Path, workspace: Path) -> None:
    snapshot.backup(source_db, backup_file)
    _init_db(restored_db, workspace)
    snapshot.restore(backup_file, restored_db)


def test_full_roundtrip_preserves_all_fields(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    restored_db = tmp_path / "restored.db"
    backup_file = tmp_path / "backup.db"
    _seed_claims(source_db, tmp_path)

    _roundtrip(source_db, restored_db, backup_file, tmp_path)

    assert _table_rows(restored_db, "claims") == _table_rows(source_db, "claims")


def test_roundtrip_preserves_citations(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    restored_db = tmp_path / "restored.db"
    backup_file = tmp_path / "backup.db"
    _seed_claims(source_db, tmp_path)

    _roundtrip(source_db, restored_db, backup_file, tmp_path)

    assert _table_rows(restored_db, "citations") == _table_rows(source_db, "citations")


def test_roundtrip_preserves_events(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    restored_db = tmp_path / "restored.db"
    backup_file = tmp_path / "backup.db"
    _seed_claims(source_db, tmp_path)

    _roundtrip(source_db, restored_db, backup_file, tmp_path)

    assert _table_rows(restored_db, "events") == _table_rows(source_db, "events")


def test_restore_into_dirty_db_fails_cleanly(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    dirty_db = tmp_path / "dirty.db"
    backup_file = tmp_path / "backup.db"
    _seed_claims(source_db, tmp_path)
    _seed_claims(dirty_db, tmp_path)
    snapshot.backup(source_db, backup_file)

    with pytest.raises(ValueError, match="not empty"):
        snapshot.restore(backup_file, dirty_db)
