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


def _memory_file(memory_dir: Path, filename: str, body: str) -> None:
    (memory_dir / filename).write_text(
        "\n".join(
            [
                "---",
                f'name: "{Path(filename).stem}"',
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


def _claim_texts(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT text FROM claims ORDER BY id").fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _ingest_payload(tmp_path: Path, payload: str) -> list[str]:
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _memory_file(memory_dir, "sensitive.md", payload)

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        dream_ingest(str(db_path))

    return _claim_texts(db_path)


def test_dream_bridge_filters_api_key(tmp_path: Path) -> None:
    secret = "OPENAI_API_KEY=sk-fake-test-1234567890abcdefghij"
    texts = _ingest_payload(tmp_path, f"Deployment note included {secret}.")

    assert texts == [] or all("sk-fake-test-1234567890abcdefghij" not in text for text in texts)


def test_dream_bridge_filters_bearer_token(tmp_path: Path) -> None:
    secret = "Authorization: Bearer eyJ-fake-jwt-token-here"
    texts = _ingest_payload(tmp_path, f"Request debugging note included {secret}.")

    assert texts == [] or all("eyJ-fake-jwt-token-here" not in text for text in texts)


def test_dream_bridge_filters_private_ip(tmp_path: Path) -> None:
    private_ip = "192.168.1.42"
    texts = _ingest_payload(tmp_path, f"The staging callback host was {private_ip}.")

    assert texts == [] or all(private_ip not in text for text in texts)
