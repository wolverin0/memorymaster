"""Tests for llm_steward auto-validation integration.

Verifies that after LLM extraction confirms claims, deterministic validators
are automatically run on the newly confirmed claims.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from memorymaster.llm_steward import _auto_validate_claims, run_steward
from memorymaster.service import MemoryService


def _make_db(prefix: str) -> str:
    """Create a temporary SQLite DB with the full schema via MemoryService."""
    fd, path = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(path).unlink(missing_ok=True)

    svc = MemoryService(path, workspace_root=Path.cwd())
    svc.init_db()
    return path


def _insert_candidate(db_path: str, text: str, claim_id: int | None = None) -> int:
    conn = sqlite3.connect(db_path)
    if claim_id:
        conn.execute(
            "INSERT INTO claims (id, text, status, created_at, updated_at) "
            "VALUES (?, ?, 'candidate', datetime('now'), datetime('now'))",
            (claim_id, text),
        )
        result_id = claim_id
    else:
        cursor = conn.execute(
            "INSERT INTO claims (text, status, created_at, updated_at) "
            "VALUES (?, 'candidate', datetime('now'), datetime('now'))",
            (text,),
        )
        result_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return result_id


def _get_claim_status(db_path: str, claim_id: int) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, confidence, subject, predicate, object_value FROM claims WHERE id = ?",
        (claim_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def _count_events(db_path: str, claim_id: int, event_type: str) -> int:
    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE claim_id = ? AND event_type = ?",
        (claim_id, event_type),
    ).fetchone()[0]
    conn.close()
    return count


def test_auto_validate_runs_deterministic_on_confirmed_claims() -> None:
    """After LLM extraction confirms a claim with ip_address predicate,
    auto-validation should run deterministic checks and adjust confidence."""
    db_path = _make_db("auto-validate")

    # Insert a confirmed claim with a valid IP predicate directly
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO claims (text, subject, predicate, object_value, status, confidence, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'confirmed', 0.6, datetime('now'), datetime('now'))",
        ("Server IP is 10.0.0.1", "server", "ip_address", "10.0.0.1"),
    )
    conn.commit()
    claim_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    result = _auto_validate_claims(db_path, [claim_id], workspace_root=str(Path.cwd()))

    assert result["checked"] >= 1
    # A valid IP should get a confidence boost
    claim = _get_claim_status(db_path, claim_id)
    assert claim["confidence"] > 0.6, "Valid IP should boost confidence"


def test_auto_validate_hard_conflicts_invalid_format() -> None:
    """An invalid IP address should result in a hard conflict."""
    db_path = _make_db("auto-validate-conflict")

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO claims (text, subject, predicate, object_value, status, confidence, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'confirmed', 0.6, datetime('now'), datetime('now'))",
        ("Server IP is not-an-ip", "server", "ip_address", "not-an-ip"),
    )
    conn.commit()
    claim_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    result = _auto_validate_claims(db_path, [claim_id], workspace_root=str(Path.cwd()))

    assert result["hard_conflicted"] >= 1


def test_auto_validate_empty_ids_is_noop() -> None:
    """Passing empty claim_ids should return immediately with zeros."""
    db_path = _make_db("auto-validate-empty")
    result = _auto_validate_claims(db_path, [], workspace_root=str(Path.cwd()))
    assert result["checked"] == 0


def test_auto_validate_skipped_on_dry_run() -> None:
    """Auto-validation should not run during dry_run."""
    db_path = _make_db("auto-validate-dryrun")
    _insert_candidate(db_path, "The server runs on port 8080 at 10.0.0.1")

    fake_extraction = {
        "subject": "server",
        "predicate": "ip_address",
        "object_value": "10.0.0.1",
        "confidence": 0.9,
        "action": "confirm",
    }

    with patch("memorymaster.llm_steward._call_llm") as mock_llm:
        mock_llm.return_value = f'[{__import__("json").dumps(fake_extraction)}]'
        stats = run_steward(
            db_path, api_key="fake", provider="gemini",
            dry_run=True, delay=0, auto_validate=True,
        )

    # In dry_run mode, auto_validation should not be present
    assert "auto_validation" not in stats


def test_auto_validate_disabled_via_flag() -> None:
    """When auto_validate=False, no validation should run."""
    db_path = _make_db("auto-validate-disabled")
    _insert_candidate(db_path, "The server runs on port 8080 at 10.0.0.1")

    fake_extraction = {
        "subject": "server",
        "predicate": "ip_address",
        "object_value": "10.0.0.1",
        "confidence": 0.9,
        "action": "confirm",
    }

    with patch("memorymaster.llm_steward._call_llm") as mock_llm:
        mock_llm.return_value = f'[{__import__("json").dumps(fake_extraction)}]'
        stats = run_steward(
            db_path, api_key="fake", provider="gemini",
            dry_run=False, delay=0, auto_validate=False,
        )

    assert stats["confirmed"] >= 1
    assert "auto_validation" not in stats


def test_auto_validate_runs_after_llm_extraction() -> None:
    """Full pipeline: LLM extracts + confirms -> auto-validate runs."""
    db_path = _make_db("auto-validate-pipeline")
    _insert_candidate(db_path, "The API endpoint is https://api.example.com/v1")

    fake_extraction = {
        "subject": "API",
        "predicate": "url",
        "object_value": "https://api.example.com/v1",
        "confidence": 0.85,
        "action": "confirm",
    }

    with patch("memorymaster.llm_steward._call_llm") as mock_llm:
        mock_llm.return_value = f'[{__import__("json").dumps(fake_extraction)}]'
        stats = run_steward(
            db_path, api_key="fake", provider="gemini",
            dry_run=False, delay=0, auto_validate=True,
            workspace_root=str(Path.cwd()),
        )

    assert stats["confirmed"] >= 1
    assert "auto_validation" in stats
    av = stats["auto_validation"]
    assert av["checked"] >= 1
    # Valid URL should boost confidence
    assert av.get("hard_conflicted", 0) == 0
