"""Contracts for lifecycle-safe scheduled archival (MM-LIFE-01)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import scheduled_archive
from memorymaster.recall import qdrant_outbox, query_cache


HOOK_TEMPLATE = (
    Path(__file__).parents[1]
    / "memorymaster"
    / "config_templates"
    / "hooks"
    / "memorymaster-steward-cycle.py"
)


def record_judgment(ledger: Path, claim_id: int) -> None:
    """Minimal contract-shaped S1 judgment (full coverage: judgment-gate tests)."""
    conn = sqlite3.connect(ledger)
    conn.executescript(
        "CREATE TABLE decisions(decision_id TEXT PRIMARY KEY, ts TEXT, surface TEXT, mode TEXT,"
        " fallback_reason TEXT, action_taken TEXT);"
        "CREATE TABLE decision_items(decision_id TEXT, item_ref TEXT, question_id TEXT);"
    )
    conn.execute(
        "INSERT INTO decisions VALUES ('d1', ?, 'REVALIDATE', 'live', NULL, 'no_longer_useful')",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.execute("INSERT INTO decision_items VALUES ('d1', ?, 'lifecycle.useful_future')", (str(claim_id),))
    conn.commit()
    conn.close()


def test_scheduled_archive_contains_no_raw_claim_status_update() -> None:
    """Scheduled archival must enter through lifecycle authority and its events."""
    source = HOOK_TEMPLATE.read_text(encoding="utf-8")
    normalized = " ".join(source.lower().split())

    assert "update claims set status = 'archived'" not in normalized


def test_scheduled_archive_preserves_lifecycle_and_vector_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        qdrant_outbox.ENV_OUTBOX_DIR,
        str(tmp_path / "qdrant-outbox"),
    )
    monkeypatch.delenv("QDRANT_URL", raising=False)
    ledger = tmp_path / "decisions.db"
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(ledger))
    db_path = tmp_path / "scheduled-archive.db"
    service = MemoryService(db_path)
    service.init_db()
    claim = service.ingest(
        "scheduled lifecycle archival evidence",
        [CitationInput(source="test://scheduled-archive")],
    )
    lifecycle.transition_claim(
        service.store,
        claim.id,
        "confirmed",
        reason="fixture confirmed",
        event_type="validator",
    )
    lifecycle.transition_claim(
        service.store,
        claim.id,
        "stale",
        reason="fixture stale",
        event_type="decay",
    )
    old_created_at = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).replace(microsecond=0).isoformat()
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE claims SET created_at = ?, updated_at = ?, access_count = 0 WHERE id = ?",
            (old_created_at, old_created_at, claim.id),
        )
        conn.commit()

    before = service.store.get_claim(claim.id, include_citations=False)
    assert before is not None
    generation_before = query_cache.read_generation(db_path)
    service.qdrant = MagicMock()
    service.qdrant.delete_claim.return_value = False
    # F-20: archival requires a recorded live S1 no_longer_useful judgment.
    record_judgment(ledger, claim.id)

    result = scheduled_archive.run(service, older_than_days=14)

    archived = service.store.get_claim(claim.id, include_citations=False)
    assert result == {"eligible": 1, "matched": 1, "archived": 1, "skipped": 0, "unjudged": 0}
    assert archived is not None
    assert archived.status == "archived"
    assert archived.version == before.version + 1
    assert archived.archived_at is not None
    assert archived.updated_at != before.updated_at
    events = service.store.list_events(
        claim_id=claim.id,
        event_type="staleness",
        limit=10,
    )
    assert len(events) == 1
    assert events[0].from_status == "stale"
    assert events[0].to_status == "archived"
    assert query_cache.read_generation(db_path) > generation_before
    service.qdrant.delete_claim.assert_called_once_with(claim.id)
    assert qdrant_outbox.pending(db_path) == [
        {"op": "delete", "claim_id": claim.id, "content_hash": None}
    ]
