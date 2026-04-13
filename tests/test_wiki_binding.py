"""Tests for v3.4 claim↔wiki bidirectional binding."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _fresh_store(tmp: Path):
    from memorymaster.storage import SQLiteStore

    db = tmp / "memory.db"
    store = SQLiteStore(str(db))
    store.init_db()
    return store, db


def _insert_claim(db: Path, text: str, scope: str = "project:test", claim_type: str = "fact") -> int:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """INSERT INTO claims (text, claim_type, subject, predicate, scope, status,
                               confidence, created_at, updated_at, valid_from, tier, version)
           VALUES (?, ?, ?, ?, ?, 'candidate', 0.5, '2026-01-01', '2026-01-01',
                   '2026-01-01', 'working', 1)""",
        (text, claim_type, "qdrant", "is", scope),
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


def test_schema_has_wiki_article_column(tmp_path: Path) -> None:
    _, db = _fresh_store(tmp_path)
    conn = sqlite3.connect(str(db))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(claims)").fetchall()]
    conn.close()
    assert "wiki_article" in cols


def test_schema_has_wiki_article_index(tmp_path: Path) -> None:
    _, db = _fresh_store(tmp_path)
    conn = sqlite3.connect(str(db))
    idx_names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='claims'"
        ).fetchall()
    ]
    conn.close()
    assert any("wiki_article" in n for n in idx_names)


def test_migration_is_idempotent(tmp_path: Path) -> None:
    from memorymaster.storage import SQLiteStore

    db = tmp_path / "memory.db"
    SQLiteStore(str(db)).init_db()
    # Re-initialising should not error on duplicate column.
    SQLiteStore(str(db)).init_db()


def test_stamp_wiki_binding_sets_column(tmp_path: Path) -> None:
    from memorymaster.wiki_engine import _stamp_wiki_binding

    _, db = _fresh_store(tmp_path)
    cid1 = _insert_claim(db, "qdrant runs on vm")
    cid2 = _insert_claim(db, "qdrant uses 6333")

    _stamp_wiki_binding(str(db), [cid1, cid2], "qdrant")

    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT wiki_article FROM claims WHERE id = ?", (cid1,)).fetchone()
    assert row[0] == "qdrant"
    row = conn.execute("SELECT wiki_article FROM claims WHERE id = ?", (cid2,)).fetchone()
    assert row[0] == "qdrant"
    conn.close()


def test_stamp_wiki_binding_silent_on_empty(tmp_path: Path) -> None:
    from memorymaster.wiki_engine import _stamp_wiki_binding

    _, db = _fresh_store(tmp_path)
    # Empty claim_ids or empty slug must not raise and must not touch the DB.
    _stamp_wiki_binding(str(db), [], "qdrant")
    _stamp_wiki_binding(str(db), [1], "")


def test_row_to_claim_reads_wiki_article(tmp_path: Path) -> None:
    from memorymaster._storage_read import _ReadMixin

    _, db = _fresh_store(tmp_path)
    cid = _insert_claim(db, "qdrant runs on vm")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("UPDATE claims SET wiki_article = 'qdrant' WHERE id = ?", (cid,))
    conn.commit()
    row = conn.execute("SELECT * FROM claims WHERE id = ?", (cid,)).fetchone()
    conn.close()

    claim = _ReadMixin._row_to_claim(row)
    assert claim.wiki_article == "qdrant"


def test_recall_appends_wiki_pointer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recall formatter should append `(compiled in [[slug]])` for bound claims."""
    from memorymaster import context_hook
    from memorymaster.models import Claim

    sample = Claim(
        id=1,
        text="Qdrant is deployed on an Ubuntu VM",
        idempotency_key=None,
        normalized_text=None,
        claim_type="fact",
        subject="qdrant",
        predicate="runs_on",
        object_value="vm",
        scope="project:test",
        volatility="medium",
        status="confirmed",
        confidence=0.9,
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-01-01",
        updated_at="2026-01-01",
        last_validated_at=None,
        archived_at=None,
        wiki_article="qdrant",
    )

    class _FakeService:
        def query_rows(self, **_: object) -> list[dict]:
            return [{"claim": sample, "lexical_score": 1.0, "confidence_score": 0.9}]

    def _fake_ctor(db_target: str, workspace_root: Path):  # noqa: ARG001
        return _FakeService()

    monkeypatch.setattr("memorymaster.service.MemoryService", _fake_ctor)

    out = context_hook.recall("qdrant", db_path=str(tmp_path / "nope.db"), skip_qdrant=True)
    assert "[[qdrant]]" in out
    assert "compiled in" in out


def test_backfill_bindings_updates_claims_from_frontmatter(tmp_path: Path) -> None:
    """wiki-backfill-bindings reads `claims: [...]` frontmatter and stamps each claim."""
    from memorymaster.cli_handlers_curation import _handle_wiki_backfill_bindings

    _, db = _fresh_store(tmp_path)
    c1 = _insert_claim(db, "qdrant runs on vm")
    c2 = _insert_claim(db, "qdrant uses 6333")
    c3 = _insert_claim(db, "other fact", scope="project:test")

    wiki = tmp_path / "vault"
    scope_dir = wiki / "project-test"
    scope_dir.mkdir(parents=True)
    (scope_dir / "qdrant.md").write_text(
        "---\n"
        f"claims: [{c1}, {c2}]\n"
        "type: fact\n"
        "---\n\n# Qdrant\n",
        encoding="utf-8",
    )

    class _Args:
        output = str(wiki)
        json_output = False

    rc = _handle_wiki_backfill_bindings(_Args(), None, None, str(db))
    assert rc == 0

    conn = sqlite3.connect(str(db))
    rows = dict(conn.execute("SELECT id, wiki_article FROM claims").fetchall())
    conn.close()
    assert rows[c1] == "qdrant"
    assert rows[c2] == "qdrant"
    assert rows[c3] is None  # not listed in any article
