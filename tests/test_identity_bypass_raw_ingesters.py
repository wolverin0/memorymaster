"""Adversarial namespace tests for legacy raw claim-ingest surfaces."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from memorymaster.bridges.dream_bridge import dream_ingest
from memorymaster.core.models import CitationInput
from memorymaster.knowledge.transcript_miner import mine_transcript
from memorymaster.stores.storage import SQLiteStore


CITATIONS = [CitationInput(source="identity-bypass-red", locator="raw-ingest")]


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.init_db()
    return store


def _private_claim(store: SQLiteStore, *, text: str, key: str) -> None:
    store.create_claim(
        text,
        CITATIONS,
        idempotency_key=key,
        subject="hidden-subject",
        predicate="uses",
        scope="project",
        source_agent="alice",
        visibility="private",
    )


def _identity_rows(store: SQLiteStore, key: str) -> list[dict[str, object]]:
    with store.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM claims WHERE idempotency_key=? ORDER BY id",
                (key,),
            )
        ]


def _write_dream_note(memory_dir: Path, filename: str) -> None:
    memory_dir.mkdir()
    (memory_dir / filename).write_text(
        "\n".join(
            [
                "---",
                'name: "public-dream-note"',
                'description: "Identity namespace regression fixture"',
                'type: "project"',
                "---",
                "",
                "The public retrieval bridge uses a bounded cache.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_dream_ingest_private_key_does_not_block_public_identity(
    tmp_path: Path,
) -> None:
    """A hidden key must not make the public dream importer report duplicate."""
    db_path = tmp_path / "dream-namespace.db"
    store = _store(db_path)
    marker = "auto-dream:public-dream.md"
    _private_claim(store, text="Alice hidden dream payload.", key=marker)
    memory_dir = tmp_path / "memory"
    _write_dream_note(memory_dir, "public-dream.md")

    with patch.dict("os.environ", {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        stats = dream_ingest(str(db_path), use_spool=False)

    rows = _identity_rows(store, marker)
    assert stats["ingested"] == 1
    assert {(row["visibility"], row["source_agent"]) for row in rows} == {
        ("private", "alice"),
        ("public", None),
    }


def test_transcript_miner_private_key_does_not_block_public_identity(
    tmp_path: Path,
) -> None:
    """Transcript dedup must query the public namespace, not every hidden row."""
    db_path = tmp_path / "transcript-namespace.db"
    store = _store(db_path)
    text = "The root cause was a stale cache entry in the retrieval worker."
    digest = hashlib.sha256(text[:500].strip().lower().encode()).hexdigest()[:16]
    key = f"transcript-{digest}"
    _private_claim(store, text="Alice hidden transcript payload.", key=key)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"role": "assistant", "content": text}) + "\n",
        encoding="utf-8",
    )

    stats = mine_transcript(
        transcript,
        str(db_path),
        scope="project",
        min_length=10,
    )

    rows = _identity_rows(store, key)
    assert stats["ingested"] == 1
    assert stats["duplicates"] == 0
    assert {(row["visibility"], row["source_agent"]) for row in rows} == {
        ("private", "alice"),
        ("public", "transcript-miner"),
    }
