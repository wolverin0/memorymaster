from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memorymaster.core.models import CitationInput
from memorymaster.core.security import (
    sanitize_claim_input,
    sanitize_claim_structure_input,
    sanitize_persisted_text,
)
from memorymaster.stores.storage import SQLiteStore


def test_claim_policy_redacts_private_ips_in_every_content_field() -> None:
    values = ("10.1.2.3", "172.16.2.4", "192.168.3.5", "10.6.7.8", "172.31.9.10")
    result = sanitize_claim_input(
        text=f"Topology uses {values[0]}",
        subject=f"host {values[1]}",
        predicate=f"routes through {values[2]}",
        object_value=f"backup {values[3]}",
        citations=[CitationInput("test", excerpt=f"observed {values[4]}")],
    )

    rendered = "\n".join(
        (result.text, result.subject or "", result.predicate or "", result.object_value or "",
         result.citations[0].excerpt or "")
    )
    assert all(value not in rendered for value in values)
    assert rendered.count("[REDACTED:private_ipv4]") == len(values)
    assert result.is_sensitive and result.findings == ["private_ipv4"]


def test_claim_policy_redacts_absolute_windows_and_unc_paths() -> None:
    drive_path = "Q:\\Synthetic User\\Private Project\\artifact.txt"
    unc_path = "\\\\private-host\\operator-share\\artifact.txt"
    result = sanitize_claim_structure_input(
        claim_type="fact",
        subject=f"workspace {drive_path}",
        predicate="stored_at",
        object_value=unc_path,
    )

    rendered = "\n".join((result.subject or "", result.object_value or ""))
    assert drive_path not in rendered and unc_path not in rendered
    assert "[REDACTED:absolute_path_windows]" in rendered
    assert "[REDACTED:absolute_path_unc]" in rendered


def test_claim_policy_allows_repo_relative_paths() -> None:
    text = "Read _intel/briefs/task.md, scripts/run.py, and runs/evaluation/result.json."
    result = sanitize_claim_input(text=text, object_value=None, citations=[])

    assert result.text == text
    assert not result.is_sensitive
    assert result.findings == []


def test_raw_source_policy_remains_unchanged() -> None:
    text = "Raw source selected at 10.1.2.3 and Q:\\Synthetic User\\artifact.txt"

    assert sanitize_persisted_text(text) == (text, [])


def test_direct_sqlite_claim_write_never_persists_private_context(tmp_path: Path) -> None:
    db_path = tmp_path / "private-context.db"
    store = SQLiteStore(db_path)
    store.init_db()
    private_ip = "192.168.44.12"
    local_path = "Q:\\Synthetic User\\Private Project\\artifact.txt"

    claim = store.create_claim(
        text=f"Mount {private_ip} from {local_path}",
        citations=[CitationInput("unit-test", excerpt=f"Seen at {private_ip}")],
        scope="project:intake-policy",
        source_agent="intake-policy-test",
    )

    with sqlite3.connect(db_path) as conn:
        payload = conn.execute(
            "SELECT payload_json FROM events WHERE claim_id=? AND details='sensitive_redaction_applied'",
            (claim.id,),
        ).fetchone()[0]
        persisted = "\n".join(
            row[0] for row in conn.execute(
                "SELECT text FROM claims WHERE id=? UNION ALL "
                "SELECT COALESCE(excerpt, '') FROM citations WHERE claim_id=?",
                (claim.id, claim.id),
            )
        )
    assert private_ip not in persisted and local_path not in persisted
    assert json.loads(payload)["findings"] == ["absolute_path_windows", "private_ipv4"]
