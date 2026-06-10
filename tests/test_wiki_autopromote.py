from __future__ import annotations

import sqlite3
from pathlib import Path

from memorymaster.lifecycle import transition_claim
from memorymaster.stores.storage import SQLiteStore
from memorymaster.knowledge.wiki_engine import absorb_single_claim


def _fresh_store(tmp_path: Path) -> tuple[SQLiteStore, Path]:
    db = tmp_path / "memory.db"
    store = SQLiteStore(str(db))
    store.init_db()
    return store, db


def _insert_claim(
    db: Path,
    *,
    text: str = "Qdrant runs on the validation host",
    subject: str = "qdrant",
    scope: str = "project:test",
    status: str = "candidate",
) -> int:
    conn = sqlite3.connect(str(db))
    cur = conn.execute(
        """INSERT INTO claims (text, claim_type, subject, predicate, object_value,
                               scope, status, confidence, created_at, updated_at,
                               valid_from, tier, version)
           VALUES (?, 'fact', ?, 'runs_on', 'validation host', ?, ?, 0.8,
                   '2026-01-01', '2026-01-01', '2026-01-01', 'working', 1)""",
        (text, subject, scope, status),
    )
    conn.commit()
    claim_id = int(cur.lastrowid)
    conn.close()
    return claim_id


def test_validator_threshold_crossing_absorbs_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, db = _fresh_store(tmp_path)
    claim_id = _insert_claim(db)
    calls: list[tuple[int, str | None]] = []

    def fake_absorb_single_claim(claim_id_arg: int, db_path: str | None = None) -> dict:
        calls.append((claim_id_arg, db_path))
        return {"absorbed": True}

    monkeypatch.setenv("MEMORYMASTER_WIKI_AUTOPROMOTE_THRESHOLD", "3")
    monkeypatch.setattr("memorymaster.knowledge.wiki_engine.absorb_single_claim", fake_absorb_single_claim)

    transition_claim(store, claim_id, "confirmed", reason="first validation", event_type="validator")
    transition_claim(store, claim_id, "stale", reason="second validation", event_type="validator")
    assert calls == []

    transition_claim(store, claim_id, "confirmed", reason="third validation", event_type="validator")

    assert calls == [(claim_id, str(db))]


def test_validator_threshold_zero_disables_autopromote(tmp_path: Path, monkeypatch) -> None:
    store, db = _fresh_store(tmp_path)
    claim_id = _insert_claim(db)
    calls: list[int] = []

    monkeypatch.setenv("MEMORYMASTER_WIKI_AUTOPROMOTE_THRESHOLD", "0")
    monkeypatch.setattr(
        "memorymaster.knowledge.wiki_engine.absorb_single_claim",
        lambda claim_id_arg, db_path=None: calls.append(claim_id_arg),
    )

    transition_claim(store, claim_id, "confirmed", reason="first validation", event_type="validator")
    transition_claim(store, claim_id, "stale", reason="second validation", event_type="validator")
    transition_claim(store, claim_id, "confirmed", reason="third validation", event_type="validator")

    assert calls == []


def test_absorb_single_claim_writes_article_and_binding(tmp_path: Path) -> None:
    _, db = _fresh_store(tmp_path)
    claim_id = _insert_claim(db, status="confirmed")
    wiki = tmp_path / "wiki"

    result = absorb_single_claim(claim_id, db_path=db, wiki_dir=wiki)

    article = wiki / "project-test" / "qdrant.md"
    assert result["absorbed"] is True
    assert result["claim_id"] == claim_id
    assert article.exists()
    content = article.read_text(encoding="utf-8")
    assert f"claims: [{claim_id}]" in content
    assert "Qdrant" in content

    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT wiki_article FROM claims WHERE id = ?", (claim_id,)).fetchone()
    conn.close()
    assert row[0] == "qdrant"
