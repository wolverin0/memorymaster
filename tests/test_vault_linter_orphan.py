from __future__ import annotations

import sqlite3
from pathlib import Path

from memorymaster.vault_linter import lint_vault


def _fresh_db(tmp_path: Path) -> Path:
    from memorymaster.storage import SQLiteStore

    db = tmp_path / "memory.db"
    SQLiteStore(str(db)).init_db()
    return db


def _write_article(vault: Path, slug: str, body: str = "") -> Path:
    scope_dir = vault / "project-test"
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / f"{slug}.md"
    path.write_text(
        "---\n"
        f"title: {slug}\n"
        "scope: project:test\n"
        "type: note\n"
        "---\n\n"
        f"# {slug}\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def _insert_claim(db: Path, *, wiki_article: str | None = None) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """INSERT INTO claims (
                   text, claim_type, subject, predicate, object_value, scope,
                   status, confidence, created_at, updated_at, valid_from,
                   tier, wiki_article
               )
               VALUES (
                   'A is referenced by a claim', 'fact', 'A', 'is', 'referenced',
                   'project:test', 'candidate', 0.8, '2026-01-01',
                   '2026-01-01', '2026-01-01', 'working', ?
               )""",
            (wiki_article,),
        )
        conn.commit()
    finally:
        conn.close()


def _orphan_article_paths(report: dict) -> set[str]:
    return {item["relative_path"] for item in report["orphan_articles"]}


def test_isolated_article_is_orphan(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    vault = tmp_path / "wiki"
    _write_article(vault, "A")

    report = lint_vault(str(db), verify_with_llm=False, wiki_root=vault)

    assert "project-test/A.md" in _orphan_article_paths(report)
    orphan = report["orphan_articles"][0]
    assert orphan["severity"] == "warning"
    assert orphan["type"] == "orphan_article"


def test_linked_article_not_orphan(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    vault = tmp_path / "wiki"
    _write_article(vault, "A")
    _write_article(vault, "B", "This article links to [[A]].")

    report = lint_vault(str(db), verify_with_llm=False, wiki_root=vault)

    assert "project-test/A.md" not in _orphan_article_paths(report)


def test_claim_referenced_article_not_orphan(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    vault = tmp_path / "wiki"
    _write_article(vault, "A")
    _insert_claim(db, wiki_article="A")

    report = lint_vault(str(db), verify_with_llm=False, wiki_root=vault)

    assert "project-test/A.md" not in _orphan_article_paths(report)
