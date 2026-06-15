"""Regression tests for wiki_engine absorb-path audit fixes.

Each test encodes WHY the behavior matters (intent), not just what the code
does today:

- HIGH: re-absorbing an existing article must feed the LLM the real compiled
  PROSE, never the YAML frontmatter — otherwise compiled truth regresses to
  metadata on every pass.
- MEDIUM: an LLM budget abort must propagate so absorb() emits its `aborted`
  metadata, instead of silently producing empty bodies that skip subjects
  while the run reports success.
- LOW: the wiki_article binding writer must use WAL + busy_timeout so a
  concurrent writer doesn't drop the binding via an immediate SQLITE_BUSY.
- LOW: breakdown must only count the entities the LLM actually selected, not
  blanket-absorb the whole scope and misreport the total.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.knowledge import wiki_engine
from memorymaster.govern import llm_budget


def _seed_claims(db: Path, subject: str, scope: str, n: int = 2) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY, text TEXT, claim_type TEXT, subject TEXT,
            predicate TEXT, object_value TEXT, scope TEXT, confidence REAL,
            status TEXT, human_id TEXT, created_at TEXT, updated_at TEXT,
            event_time TEXT, wiki_article TEXT)"""
    )
    for i in range(n):
        conn.execute(
            "INSERT INTO claims (text, claim_type, subject, predicate, scope, "
            "confidence, status) VALUES (?,?,?,?,?,?,?)",
            (f"new fact {i} about {subject}", "fact", subject, "is", scope, 0.9, "confirmed"),
        )
    conn.commit()
    conn.close()


def test_update_feeds_compiled_prose_not_frontmatter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-absorb must hand the model the existing compiled-truth prose.

    If it feeds the YAML frontmatter instead, the rewrite is seeded from
    title/tags/claims metadata and the real prose is discarded — compiled
    truth regresses every pass. This anchors on that requirement.
    """
    db = tmp_path / "m.db"
    _seed_claims(db, "Widget", "project:demo")

    wiki_dir = tmp_path / "wiki"
    scope_dir = wiki_engine._scope_dirname("project:demo")
    art_dir = wiki_dir / scope_dir
    art_dir.mkdir(parents=True)
    sentinel = "ORIGINAL_PROSE_THAT_MUST_SURVIVE_REABSORB"
    (art_dir / "widget.md").write_text(
        "---\n"
        "title: Widget\n"
        'tags: ["FRONTMATTER_ONLY_TAG"]\n'
        "claims: [1, 2]\n"
        "---\n\n"
        "# Widget\n\n"
        f"## Summary\n{sentinel}\n\n"
        "---\n\n"
        "## Timeline\n\n### 2026-01-01 | fact\nold entry\n",
        encoding="utf-8",
    )

    captured: dict[str, str] = {}

    def spy(prompt: str, text: str) -> str:
        captured["prompt"] = prompt
        return "Rewritten compiled truth that is comfortably longer than fifty characters."

    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", spy)
    wiki_engine.absorb(str(db), wiki_dir, scope_filter="project:demo")

    prompt = captured["prompt"]
    assert sentinel in prompt, "existing compiled prose was not fed to the LLM"
    assert "FRONTMATTER_ONLY_TAG" not in prompt, "frontmatter leaked into the prompt"
    assert "claims: [1, 2]" not in prompt, "raw frontmatter claims list leaked in"


def test_budget_abort_propagates_through_call_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget abort must NOT be swallowed to an empty string.

    Swallowing it lets the remaining subjects get empty bodies and be
    silently skipped while the run still reports success. The abort has to
    bubble so absorb() can emit its documented `aborted` metadata.
    """

    def boom(prompt: str, text: str) -> str:
        raise llm_budget.LLMBudgetExceeded(reason="cap", provider="gemini")

    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", boom)
    with pytest.raises(llm_budget.LLMBudgetExceeded):
        wiki_engine._call_llm("prompt", "text")


def test_generic_llm_error_still_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-budget failures still degrade to '' so absorb continues."""

    def transient(prompt: str, text: str) -> str:
        raise RuntimeError("network blip")

    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", transient)
    assert wiki_engine._call_llm("p", "t") == ""


def test_absorb_aborts_with_metadata_on_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: when the budget trips mid-absorb, absorb reports aborted.

    This is the user-visible consequence of the MEDIUM fix — the run must
    NOT report a clean success that hides skipped subjects.
    """
    db = tmp_path / "m.db"
    _seed_claims(db, "Widget", "project:demo")

    def boom(prompt: str, text: str) -> str:
        raise llm_budget.LLMBudgetExceeded(reason="cap", provider="gemini")

    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", boom)
    # No parent budget scope active -> absorb opens its own and catches abort.
    result = wiki_engine.absorb(str(db), tmp_path / "wiki", scope_filter="project:demo")
    assert result.get("aborted") is True
    assert result.get("aborted_reason") == "cap"


def test_stamp_binding_uses_wal_and_persists(tmp_path: Path) -> None:
    """The binding writer must enable WAL and actually write the slug.

    Without WAL + busy_timeout a concurrent writer yields immediate
    SQLITE_BUSY and the binding is silently dropped.
    """
    db = tmp_path / "b.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY, wiki_article TEXT)")
    conn.execute("INSERT INTO claims (id) VALUES (7)")
    conn.commit()
    conn.close()

    wiki_engine._stamp_wiki_binding(str(db), [7], "gizmo")

    conn = sqlite3.connect(str(db))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    val = conn.execute("SELECT wiki_article FROM claims WHERE id=7").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"
    assert val == "gizmo"


def test_breakdown_resolves_selected_entity_claim(tmp_path: Path) -> None:
    """breakdown must turn an LLM-selected entity into its own claim id.

    The old code read the selection then ran a blanket scope-wide absorb and
    misreported the count. The resolver underpins the per-entity fix.
    """
    db = tmp_path / "r.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY, subject TEXT, scope TEXT, "
        "confidence REAL, status TEXT)"
    )
    conn.execute("INSERT INTO claims VALUES (1,'Gizmo','project:demo',0.4,'confirmed')")
    conn.execute("INSERT INTO claims VALUES (2,'Gizmo','project:demo',0.95,'confirmed')")
    conn.execute("INSERT INTO claims VALUES (3,'Other','project:demo',0.95,'confirmed')")
    conn.commit()
    conn.close()

    cid = wiki_engine._resolve_subject_claim_id(str(db), "Gizmo", "project:demo")
    assert cid == 2  # highest-confidence claim for the selected subject
    assert wiki_engine._resolve_subject_claim_id(str(db), "Ghost", "project:demo") is None
