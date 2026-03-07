from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService
from memorymaster.storage import utc_now


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def test_event_hash_chain_is_written_and_linked() -> None:
    db = _case_db("reliability-hash-chain")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    claim = service.ingest(
        text="Support email is hashcheck@example.com",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="seed")],
        subject="support",
        predicate="email",
        object_value="hashcheck@example.com",
    )
    service.store.record_event(
        claim_id=claim.id,
        event_type="audit",
        details="integrity smoke",
        payload={"ok": True},
    )
    service.store.set_confidence(claim.id, 0.77, details="manual adjust")

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        cols = [str(row["name"]) for row in con.execute("PRAGMA table_info(events)").fetchall()]
        assert "prev_event_hash" in cols
        assert "event_hash" in cols
        assert "hash_algo" in cols

        rows = con.execute(
            "SELECT id, prev_event_hash, event_hash, hash_algo FROM events ORDER BY id ASC"
        ).fetchall()
    finally:
        con.close()

    assert len(rows) >= 3
    previous_hash: str | None = None
    for row in rows:
        event_hash = row["event_hash"]
        prev_hash = row["prev_event_hash"]
        algo = row["hash_algo"]
        assert isinstance(event_hash, str) and len(event_hash) == 64
        assert algo == "sha256-v1"
        assert prev_hash == previous_hash
        previous_hash = event_hash


def test_reconcile_integrity_reports_and_fixes_orphans_and_chain() -> None:
    db = _case_db("reliability-reconcile")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    claim = service.ingest(
        text="Release deadline is 2026-06-01",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="deadline")],
        subject="release",
        predicate="deadline",
        object_value="2026-06-01",
    )

    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute(
            """
            INSERT INTO events (
                claim_id, event_type, from_status, to_status, details, payload_json, created_at,
                prev_event_hash, event_hash, hash_algo
            ) VALUES (?, ?, NULL, NULL, ?, ?, ?, NULL, NULL, NULL)
            """,
            (999999, "system", "orphan event", '{"scope":"test"}', utc_now()),
        )
        con.execute(
            """
            INSERT INTO citations (claim_id, source, locator, excerpt, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (999999, "session://chat", "turn-x", "orphan citation", utc_now()),
        )
        con.execute("UPDATE claims SET status = 'superseded', replaced_by_claim_id = NULL WHERE id = ?", (claim.id,))
        con.commit()
    finally:
        con.close()

    report = service.store.reconcile_integrity(fix=False, limit=50)
    assert report["summary"]["orphan_events"] >= 1
    assert report["summary"]["orphan_citations"] >= 1
    assert report["summary"]["superseded_without_replacement"] >= 1
    assert report["summary"]["hash_chain_issues"] == 0

    fixed = service.store.reconcile_integrity(fix=True, limit=50)
    action_names = {str(action.get("action")) for action in fixed.get("actions", [])}
    assert "skip_delete_orphan_events_append_only" in action_names
    assert "delete_orphan_citations" in action_names

    after = service.store.reconcile_integrity(fix=False, limit=50)
    assert after["summary"]["orphan_events"] >= 1
    assert after["summary"]["orphan_citations"] == 0
    assert after["summary"]["hash_chain_issues"] == 0
    # Intentional: helper currently reports but does not auto-resolve semantic claim-state issues.
    assert after["summary"]["superseded_without_replacement"] >= 1

