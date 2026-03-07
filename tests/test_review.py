from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile

from memorymaster.models import CitationInput
from memorymaster.review import build_review_queue, queue_to_dicts
from memorymaster.service import MemoryService


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def test_review_queue_triages_unresolved_risk_and_sensitive_filtering(monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_ALLOW_SENSITIVE_BYPASS", "1")
    db = _case_db("sqlite-review")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    original = service.ingest(
        text="Support email is old@example.com",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="old email")],
        subject="support",
        predicate="email",
        object_value="old@example.com",
        confidence=0.90,
    )
    service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.4)

    conflicting = service.ingest(
        text="Support email is new@example.com",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="new email")],
        subject="support",
        predicate="email",
        object_value="new@example.com",
        confidence=0.40,
    )
    service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.4)

    con = sqlite3.connect(str(db))
    con.execute(
        "UPDATE claims SET last_validated_at='2025-01-01T00:00:00+00:00', updated_at='2025-01-01T00:00:00+00:00' WHERE id=?",
        (original.id,),
    )
    con.commit()
    con.close()

    service.run_cycle(policy_mode="cadence", policy_limit=50, min_citations=1, min_score=0.99)

    sensitive = service.ingest(
        text="token=sk-abcdefghijklmnopqrstuv",
        citations=[CitationInput(source="session://chat", locator="turn-3", excerpt="token update")],
        subject="auth",
        predicate="token",
        object_value="sk-abcdefghijklmnopqrstuv",
    )
    service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.4)

    claims = service.list_claims(limit=25, include_archived=True)
    status_by_id = {claim.id: claim.status for claim in claims}
    assert status_by_id[conflicting.id] == "conflicted"
    assert status_by_id[original.id] == "stale"

    queue = build_review_queue(service, limit=50)
    by_id = {item.claim_id: item for item in queue}
    ids = set(by_id)

    assert conflicting.id in ids
    assert original.id in ids
    assert sensitive.id not in ids

    assert by_id[conflicting.id].status == "conflicted"
    assert by_id[original.id].status == "stale"
    assert "status=conflicted" in by_id[conflicting.id].reason
    assert "status=stale" in by_id[original.id].reason
    assert by_id[conflicting.id].citations_count == 1
    assert by_id[original.id].citations_count == 1

    assert queue[0].claim_id == conflicting.id
    assert by_id[conflicting.id].priority > by_id[original.id].priority
    for left, right in zip(queue, queue[1:]):
        assert left.priority >= right.priority

    serialized = queue_to_dicts(queue)
    assert serialized[0]["claim_id"] == queue[0].claim_id
    assert set(serialized[0]) == {
        "claim_id",
        "status",
        "subject",
        "predicate",
        "object_value",
        "confidence",
        "updated_at",
        "reason",
        "priority",
        "citations_count",
    }

    with_sensitive = build_review_queue(service, limit=50, include_sensitive=True)
    assert sensitive.id in {item.claim_id for item in with_sensitive}

    without_stale = build_review_queue(service, limit=50, include_stale=False, include_sensitive=True)
    assert all(item.status != "stale" for item in without_stale)

    without_conflicted = build_review_queue(service, limit=50, include_conflicted=False, include_sensitive=True)
    assert all(item.status != "conflicted" for item in without_conflicted)

