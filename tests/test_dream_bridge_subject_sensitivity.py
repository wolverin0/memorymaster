"""Regression: dream_ingest must sanitize the claim SUBJECT, not just the body.

WHY THIS MATTERS: dream_ingest builds a claim from an Auto Dream memory file.
The body goes through the sensitivity filter, but the subject was taken
verbatim from the frontmatter ``name:`` field and written straight into the
claims table. A token or personal path smuggled into ``name:`` would be
persisted unencrypted on EVERY dream_sync — a direct breach of the
sensitivity-filter invariant ("the filter MUST run on every ingest path").

These tests anchor on the invariant: a sensitive subject never lands verbatim
in the DB. They do not assert any particular redaction string, only that the
secret does not survive ingest.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from memorymaster.dream_bridge import dream_ingest
from memorymaster.service import MemoryService


def _init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "memory.db"
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    return db_path


def _memory_file(memory_dir: Path, filename: str, name: str, body: str) -> None:
    (memory_dir / filename).write_text(
        "\n".join(
            [
                "---",
                f'name: "{name}"',
                'description: "Imported from test memory"',
                'type: "project"',
                "---",
                "",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )


def _rows(db_path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT subject, text FROM claims ORDER BY id").fetchall()
    finally:
        conn.close()


def _ingest(tmp_path: Path, name: str, body: str) -> list[tuple[str, str]]:
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _memory_file(memory_dir, "note.md", name, body)
    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        dream_ingest(str(db_path))
    return _rows(db_path)


def test_token_in_subject_never_persisted_verbatim(tmp_path: Path) -> None:
    token = "sk_live_" + "A" * 24
    rows = _ingest(tmp_path, name=token, body="A perfectly benign body sentence.")
    for subject, _text in rows:
        assert token not in (subject or "")


def test_benign_subject_survives_ingest(tmp_path: Path) -> None:
    rows = _ingest(
        tmp_path,
        name="Qdrant configuration",
        body="Qdrant runs as an external vector store via QDRANT_URL.",
    )
    # The benign claim is ingested and its subject preserved unchanged.
    assert len(rows) == 1
    assert rows[0][0] == "Qdrant configuration"
