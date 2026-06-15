from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from memorymaster.bridges.dream_bridge import dream_ingest, dream_seed
from memorymaster.core.service import MemoryService


def _init_db(tmp_path: Path, name: str = "memory.db") -> Path:
    db_path = tmp_path / name
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    return db_path


def _memory_file(
    memory_dir: Path,
    filename: str,
    body: str,
    *,
    name: str | None = None,
    dream_type: str = "project",
) -> Path:
    path = memory_dir / filename
    path.write_text(
        "\n".join(
            [
                "---",
                f'name: "{name or Path(filename).stem}"',
                'description: "Imported from test memory"',
                f'type: "{dream_type}"',
                "---",
                "",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _claim_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT text, claim_type, subject, idempotency_key FROM claims ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _insert_exportable_claim(db_path: Path, text: str, *, claim_id: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO claims (id, text, claim_type, subject, status, confidence, "
            "scope, volatility, idempotency_key, created_at, updated_at) "
            "VALUES (?, ?, 'fact', 'dream-bridge', 'confirmed', 0.9, "
            "'project:roundtrip', 'medium', ?, datetime('now'), datetime('now'))",
            (claim_id, text, f"roundtrip-{claim_id}"),
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(claims)").fetchall()}
        updates = ["tier = 'working'"]
        if "quality_score" in columns:
            updates.append("quality_score = 0.9")
        if "access_count" in columns:
            updates.append("access_count = 5")
        conn.execute(f"UPDATE claims SET {', '.join(updates)} WHERE id = ?", (claim_id,))
        conn.commit()
    finally:
        conn.close()


def test_empty_spool_poll_produces_no_ingests(tmp_path: Path) -> None:
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        result = dream_ingest(str(db_path))

    assert result["ingested"] == 0
    assert result["skipped"] == 0
    assert _claim_rows(db_path) == []


def test_malformed_json_spool_is_skipped_gracefully(tmp_path: Path) -> None:
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "bad-spool.json").write_text("{not valid json", encoding="utf-8")

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        result = dream_ingest(str(db_path))

    assert result["ingested"] == 0
    assert result["skipped"] == 0
    assert _claim_rows(db_path) == []


def test_multi_claim_spool_ingests_all_memory_entries(tmp_path: Path) -> None:
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    payloads = [
        "The dream bridge imports project facts.",
        "Dream memory feedback becomes a preference claim.",
        "Reference memories keep reference claim type.",
    ]
    _memory_file(memory_dir, "one.md", payloads[0], name="one")
    _memory_file(memory_dir, "two.md", payloads[1], name="two", dream_type="feedback")
    _memory_file(memory_dir, "three.md", payloads[2], name="three", dream_type="reference")

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        result = dream_ingest(str(db_path))

    rows = _claim_rows(db_path)
    assert result["ingested"] == 3
    assert [row["text"] for row in rows] == [payloads[0], payloads[2], payloads[1]]
    assert [row["claim_type"] for row in rows] == ["fact", "reference", "preference"]
    assert [row["idempotency_key"] for row in rows] == [
        "auto-dream:one.md",
        "auto-dream:three.md",
        "auto-dream:two.md",
    ]


def test_export_roundtrip_preserves_claim_payloads(tmp_path: Path) -> None:
    export_db = _init_db(tmp_path, "export.db")
    original_payloads = [
        "Roundtrip payload alpha records the cache eviction policy.",
        "Roundtrip payload beta documents the release checklist owner.",
    ]
    for offset, payload in enumerate(original_payloads, start=1):
        _insert_exportable_claim(export_db, payload, claim_id=offset)

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        seed_result = dream_seed(
            str(export_db),
            min_tier=2,
            min_quality=0.0,
            max_memories=10,
        )

    assert seed_result["seeded"] == 2
    exported_files = sorted(memory_dir.glob("mm_*.md"))
    assert len(exported_files) == 2
    for index, exported_file in enumerate(exported_files, start=1):
        exported_file.rename(memory_dir / f"external_{index}.md")

    import_db = _init_db(tmp_path, "import.db")
    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        ingest_result = dream_ingest(str(import_db))

    imported_texts = [row["text"] for row in _claim_rows(import_db)]
    assert ingest_result["ingested"] == 2
    assert len(imported_texts) == 2
    for payload in original_payloads:
        assert any(text.startswith(payload) for text in imported_texts)


def test_done_suffix_idempotence_skips_second_poll(tmp_path: Path) -> None:
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    spool_file = _memory_file(memory_dir, "claim.md", "Poll this memory once.")

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        first = dream_ingest(str(db_path))

    spool_file.rename(memory_dir / "claim.md.done")
    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        second = dream_ingest(str(db_path))

    rows = _claim_rows(db_path)
    assert first["ingested"] == 1
    assert second["ingested"] == 0
    assert second["skipped"] == 0
    assert [row["text"] for row in rows] == ["Poll this memory once."]
