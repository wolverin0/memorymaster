from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from memorymaster.dream_bridge import dream_clean, dream_ingest, dream_seed, dream_sync
from memorymaster.service import MemoryService


def _init_db(tmp_path: Path, name: str = "memory.db") -> Path:
    db_path = tmp_path / name
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    return db_path


def _memory_dir_for(home: Path, project_path: Path) -> Path:
    slug = str(project_path.resolve())
    for old, new in (("/", "-"), ("\\", "-"), (":", "-"), ("_", "-"), (" ", "-")):
        slug = slug.replace(old, new)
    return home / ".claude" / "projects" / slug.strip("-") / "memory"


def _write_memory_file(memory_dir: Path, filename: str, body: str, *, dream_type: str = "project") -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / filename).write_text(
        "\n".join(
            [
                "---",
                f'name: "{Path(filename).stem}"',
                'description: "Imported from extra coverage test"',
                f'type: "{dream_type}"',
                "---",
                "",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )


def _insert_claim(
    db_path: Path,
    claim_id: int,
    text: str,
    *,
    scope: str = "project:memorymaster",
    tier: str = "working",
    quality: float = 0.9,
    status: str = "confirmed",
    claim_type: str = "fact",
    subject: str = "dream bridge",
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO claims (id, text, claim_type, subject, status, confidence, "
            "scope, volatility, idempotency_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 0.9, ?, 'medium', ?, '2026-05-30', '2026-05-30')",
            (claim_id, text, claim_type, subject, status, scope, f"extra-{claim_id}"),
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(claims)").fetchall()}
        updates = ["tier = ?", "access_count = 10"]
        params: list[object] = [tier]
        if "quality_score" in columns:
            updates.append("quality_score = ?")
            params.append(quality)
        params.append(claim_id)
        conn.execute(f"UPDATE claims SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()


def _claim_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT text, claim_type, idempotency_key FROM claims ORDER BY id").fetchall()
    finally:
        conn.close()


def test_dream_seed_project_scope_filter_prevents_cross_project_export(tmp_path: Path) -> None:
    """Why: dream-seed must not leak another project scope into Claude memory."""
    db_path = _init_db(tmp_path)
    project_path = tmp_path / "MemoryMaster"
    home = tmp_path / "home"
    memory_dir = _memory_dir_for(home, project_path)
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("- [human.md](human.md) - keep\n", encoding="utf-8")
    _insert_claim(db_path, 1, "MemoryMaster keeps scoped export claims isolated.", scope="project:memorymaster")
    _insert_claim(db_path, 2, "Other project claim must stay out of this dream export.", scope="project:other")

    with patch("memorymaster.dream_bridge.Path.home", return_value=home):
        result = dream_seed(str(db_path), project_path=str(project_path), min_quality=0.0)

    exported = sorted(path.name for path in memory_dir.glob("mm_*.md"))
    assert result["seeded"] == 1
    assert result["total_claims"] == 1
    assert exported == ["mm_1_memorymaster-keeps-scoped-export-claims.md"]
    assert "Other project claim" not in (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "- [human.md](human.md) - keep" in (memory_dir / "MEMORY.md").read_text(encoding="utf-8")


def test_dream_seed_skips_existing_sensitive_and_near_duplicate_exports(tmp_path: Path) -> None:
    """Why: export hardening must avoid duplicate clutter and sensitive dream memories."""
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    existing_name = "mm_1_existing-exported-memory-claim.md"
    (memory_dir / existing_name).write_text("already present", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text(f"- [{existing_name}]({existing_name}) - old\n", encoding="utf-8")
    _insert_claim(db_path, 1, "Existing exported memory claim")
    _insert_claim(db_path, 2, "Existing exported memory claim again")
    _insert_claim(db_path, 3, "```bash\ncd app\nnpm install\ncurl http://localhost\n```")
    _insert_claim(db_path, 4, "Fresh export survives the filters.")

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        result = dream_seed(str(db_path), min_quality=0.0, max_memories=10)

    exported = sorted(path.name for path in memory_dir.glob("mm_*.md"))
    assert result["seeded"] == 1
    assert result["skipped"] == 3
    assert exported == [existing_name, "mm_4_fresh-export-survives-the-filters.md"]


def test_dream_seed_exports_dream_types_and_respects_max_memories(tmp_path: Path) -> None:
    """Why: seeded memory type controls how Claude applies feedback, user, and reference notes."""
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _insert_claim(db_path, 1, "Prefer small reversible patches.", claim_type="preference")
    _insert_claim(db_path, 2, "The primary user role is maintainer.", claim_type="identity")
    _insert_claim(db_path, 3, "The API reference endpoint is documented.", claim_type="reference")

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        capped = dream_seed(str(db_path), min_tier=99, min_quality=0.0, max_memories=1)
        full = dream_seed(str(db_path), min_tier=99, min_quality=0.0, max_memories=10, dry_run=True)

    assert capped["seeded"] == 1
    assert full["skipped"] == 1
    feedback = (memory_dir / "mm_1_prefer-small-reversible-patches.md").read_text(encoding="utf-8")
    assert 'type: "feedback"' in feedback
    assert "**How to apply:** When working with dream bridge" in feedback

    (memory_dir / "mm_1_prefer-small-reversible-patches.md").unlink()
    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        dream_seed(str(db_path), min_tier=99, min_quality=0.0, max_memories=10)

    exported_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(memory_dir.glob("mm_*.md")))
    assert 'type: "user"' in exported_text
    assert 'type: "reference"' in exported_text


def test_dream_seed_aborts_when_auto_dream_lock_exists(tmp_path: Path) -> None:
    """Why: seeding while Auto Dream is writing can corrupt the shared memory directory."""
    db_path = _init_db(tmp_path)
    project_path = tmp_path / "locked project"
    home = tmp_path / "home"
    lock_dir = _memory_dir_for(home, project_path).parent
    lock_dir.mkdir(parents=True)
    (lock_dir / ".dream.lock").write_text("running", encoding="utf-8")

    with patch("memorymaster.dream_bridge.Path.home", return_value=home):
        result = dream_seed(str(db_path), project_path=str(project_path))

    assert result["seeded"] == 0
    assert result["memory_dir"] == ""
    assert "lock file detected" in result["error"]


def test_dream_ingest_truncates_unknown_type_and_is_idempotent(tmp_path: Path) -> None:
    """Why: imported Auto Dream notes must stay bounded and retry-safe."""
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    long_body = "A" * 2050
    _write_memory_file(memory_dir, "unknown.md", long_body, dream_type="surprise")

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        first = dream_ingest(str(db_path))
        second = dream_ingest(str(db_path))

    rows = _claim_rows(db_path)
    assert first["ingested"] == 1
    assert second["skipped"] == 1
    assert len(rows) == 1
    assert len(rows[0]["text"]) == 2000
    assert rows[0]["claim_type"] == "fact"
    assert rows[0]["idempotency_key"] == "auto-dream:unknown.md"


def test_dream_sync_imports_first_then_exports_new_memory(tmp_path: Path) -> None:
    """Why: bidirectional sync must pull external notes before seeding MemoryMaster claims."""
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    _write_memory_file(memory_dir, "external.md", "External dream note should become a candidate.")
    _insert_claim(db_path, 10, "Confirmed MemoryMaster note should seed after ingest.")

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        result = dream_sync(str(db_path), min_quality=0.0, max_memories=10)

    assert result["ingest"]["ingested"] == 1
    assert result["seed"]["seeded"] == 1
    assert (memory_dir / "mm_10_confirmed-memorymaster-note-should-seed.md").exists()
    assert [row["idempotency_key"] for row in _claim_rows(db_path)][-1] == "auto-dream:external.md"


def test_dream_clean_removes_only_memorymaster_exports_from_index(tmp_path: Path) -> None:
    """Why: cleanup must not delete human-authored dream memories or index entries."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "mm_1_old.md").write_text("old", encoding="utf-8")
    (memory_dir / "human.md").write_text("keep", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text(
        "- [human.md](human.md) - keep\n- [mm_1_old.md](mm_1_old.md) - remove\n",
        encoding="utf-8",
    )

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        result = dream_clean(dry_run=False)

    assert result["removed"] == 1
    assert not (memory_dir / "mm_1_old.md").exists()
    assert (memory_dir / "human.md").exists()
    assert (memory_dir / "MEMORY.md").read_text(encoding="utf-8") == "- [human.md](human.md) - keep\n"
