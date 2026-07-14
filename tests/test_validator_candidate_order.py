from __future__ import annotations

import sqlite3

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import validator


def test_validator_prefers_immutable_candidate_recency(tmp_path, monkeypatch) -> None:
    db = tmp_path / "candidate-order.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    monkeypatch.setattr(validator, "load_classifier", lambda: None)

    older = service.ingest(
        text="Release deadline is 2026-04-01",
        citations=[CitationInput(source="session://test", locator="turn-1")],
        subject="release",
        predicate="deadline",
        object_value="2026-04-01",
    )
    newer = service.ingest(
        text="Release deadline moved to 2026-04-15",
        citations=[CitationInput(source="session://test", locator="turn-2")],
        subject="release",
        predicate="deadline",
        object_value="2026-04-15",
    )

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE claims SET created_at = ?, updated_at = ? WHERE id = ?",
            ("2026-04-01T00:00:00+00:00", "2026-04-16T00:00:00+00:00", older.id),
        )
        conn.execute(
            "UPDATE claims SET created_at = ?, updated_at = ? WHERE id = ?",
            ("2026-04-15T00:00:00+00:00", "2026-04-15T00:00:00+00:00", newer.id),
        )

    validator.run(service.store, min_citations=1, min_score=0.5)

    status_by_id = {claim.id: claim.status for claim in service.list_claims(limit=10)}
    assert status_by_id[newer.id] == "confirmed"
    assert status_by_id[older.id] == "conflicted"
