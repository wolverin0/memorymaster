from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def test_cli_review_queue_outputs_conflict_items() -> None:
    db = _case_db("sqlite-cli-review")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    service.ingest(
        text="Primary contact is Alice Rivera",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="first contact")],
        subject="client",
        predicate="primary_contact",
        object_value="Alice Rivera",
        confidence=0.7,
    )
    service.ingest(
        text="Primary contact is Bruno Silva",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="new contact")],
        subject="client",
        predicate="primary_contact",
        object_value="Bruno Silva",
        confidence=0.7,
    )
    service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.5)

    # Force a stale claim to verify mixed queue content.
    con = sqlite3.connect(str(db))
    con.execute(
        "UPDATE claims SET status='stale', updated_at='2025-01-01T00:00:00+00:00' WHERE subject='client' AND predicate='primary_contact' AND object_value='Alice Rivera'"
    )
    con.commit()
    con.close()

    cmd = [
        sys.executable,
        "-m",
        "memorymaster",
        "--db",
        str(db),
        "review-queue",
        "--limit",
        "50",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr

    payload = json.loads(proc.stdout)
    assert payload["rows"] >= 1
    items = payload["items"]
    assert isinstance(items, list)
    statuses = {item["status"] for item in items}
    assert "stale" in statuses or "conflicted" in statuses
    first = items[0]
    assert "priority" in first
    assert "reason" in first
    assert "citations_count" in first


