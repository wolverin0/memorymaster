"""Wiki layer: explored frontmatter + inline contradiction callouts.

Two UX patterns borrowed from shannhk/llm-wiki (claim mm-09b4):

1. ``explored: true|false`` frontmatter — operator-set human-review marker.
   Defaults to ``false`` on new articles. Preserved across re-absorb if an
   operator flipped it to ``true`` (re-upsert preservation pattern, mm-3e07).

2. Inline ``> [!contradiction]`` Obsidian callouts — when an article's claim
   cluster has (subject, predicate) groups disagreeing on object_value, render
   them inline at the top of the article body so they're visible while
   reading the wiki, not just in a separate ``lint-vault`` report.

Detection is shared with ``vault_linter._detect_contradictions`` so wiki-absorb
and lint-vault agree on what counts as a contradiction.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from memorymaster.wiki_engine import (
    _build_contradiction_callout,
    _read_existing_explored,
    _write_article,
)


# ---------------------------------------------------------------------------
# explored frontmatter
# ---------------------------------------------------------------------------


def test_new_article_defaults_explored_false(tmp_path: Path) -> None:
    fp = _write_article(
        tmp_path, "project-test", "foo", "Foo Article",
        "A short body. [[bar]]", "decision", "project:test",
        [1, 2], ["[[bar]]"],
        description="A foo decision article for testing.",
    )
    content = fp.read_text(encoding="utf-8")
    assert "explored: false" in content
    assert "explored: true" not in content


def test_explored_true_preserved_on_reabsorb(tmp_path: Path) -> None:
    """Critical: operator review survives re-absorb. Same pattern as mm-3e07."""
    fp = _write_article(
        tmp_path, "project-test", "foo", "Foo",
        "A short body. [[bar]]", "decision", "project:test",
        [1], ["[[bar]]"], description="A foo article.",
    )
    # Operator manually flips explored to true.
    content = fp.read_text(encoding="utf-8")
    fp.write_text(content.replace("explored: false", "explored: true"), encoding="utf-8")
    assert _read_existing_explored(fp) is True

    # Re-absorb (e.g. wiki-absorb runs again with new claims).
    fp2 = _write_article(
        tmp_path, "project-test", "foo", "Foo",
        "Updated body. [[bar]]", "decision", "project:test",
        [1, 2, 3], ["[[bar]]"], description="Updated foo article.",
    )
    assert "explored: true" in fp2.read_text(encoding="utf-8")


def test_read_existing_explored_returns_none_for_missing_field(tmp_path: Path) -> None:
    fp = tmp_path / "no-explored.md"
    fp.write_text(
        "---\ntitle: Test\ntype: fact\nscope: project:x\n---\n\nbody",
        encoding="utf-8",
    )
    assert _read_existing_explored(fp) is None


def test_read_existing_explored_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert _read_existing_explored(tmp_path / "nope.md") is None


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("yes", True), ("1", True),
    ("false", False), ("False", False), ("no", False), ("0", False),
])
def test_read_existing_explored_parses_bool_variants(tmp_path: Path, raw: str, expected: bool) -> None:
    fp = tmp_path / "art.md"
    fp.write_text(
        f"---\ntitle: T\ntype: f\nscope: s\nexplored: {raw}\n---\n\nbody",
        encoding="utf-8",
    )
    assert _read_existing_explored(fp) is expected


# ---------------------------------------------------------------------------
# Inline contradiction callouts
# ---------------------------------------------------------------------------


def test_contradiction_callout_renders_when_predicate_disagrees() -> None:
    claims = [
        {"id": 1, "human_id": "mm-aaa1", "subject": "foo", "predicate": "is_a",
         "object_value": "cat", "text": "foo is a cat", "confidence": 0.9},
        {"id": 2, "human_id": "mm-bbb2", "subject": "foo", "predicate": "is_a",
         "object_value": "dog", "text": "foo is a dog", "confidence": 0.7},
        {"id": 3, "human_id": "mm-ccc3", "subject": "foo", "predicate": "is_a",
         "object_value": "bird", "text": "foo is a bird", "confidence": 0.5},
    ]
    block = _build_contradiction_callout(claims)
    assert "> [!contradiction]" in block
    assert "is_a" in block
    # Higher-confidence claim listed first.
    pos_aaa1 = block.find("mm-aaa1")
    pos_ccc3 = block.find("mm-ccc3")
    assert 0 <= pos_aaa1 < pos_ccc3
    # Confidence rendered.
    assert "conf=0.90" in block


def test_no_contradiction_block_for_agreeing_claims() -> None:
    claims = [
        {"id": 1, "human_id": "mm-x", "subject": "foo", "predicate": "has",
         "object_value": "tail", "text": "foo has a tail", "confidence": 0.9},
        {"id": 2, "human_id": "mm-y", "subject": "foo", "predicate": "has",
         "object_value": "tail", "text": "foo has a tail", "confidence": 0.7},
    ]
    assert _build_contradiction_callout(claims) == ""


def test_no_contradiction_block_for_single_claim() -> None:
    claims = [
        {"id": 1, "human_id": "mm-x", "subject": "foo", "predicate": "is_a",
         "object_value": "cat", "text": "foo is a cat", "confidence": 0.9},
    ]
    assert _build_contradiction_callout(claims) == ""


def test_no_contradiction_block_for_empty_list() -> None:
    assert _build_contradiction_callout([]) == ""


def test_contradiction_block_is_obsidian_callout_syntax() -> None:
    """Every line in a callout block must start with ``> `` so Obsidian
    renders it as a single contiguous callout, not as fragmented quotes."""
    claims = [
        {"id": 1, "human_id": "mm-a", "subject": "x", "predicate": "p",
         "object_value": "alpha", "text": "x p alpha", "confidence": 0.9},
        {"id": 2, "human_id": "mm-b", "subject": "x", "predicate": "p",
         "object_value": "beta", "text": "x p beta", "confidence": 0.5},
    ]
    block = _build_contradiction_callout(claims).rstrip("\n")
    for line in block.splitlines():
        # Obsidian callouts can have a trailing empty separator line, which
        # we render as ``> ``. Any non-empty content line must also start ``> ``.
        assert line == "" or line.startswith(">"), f"non-callout line in block: {line!r}"


# ---------------------------------------------------------------------------
# validate-wiki hook recognises ``explored`` field
# ---------------------------------------------------------------------------


_HOOK = Path(__file__).resolve().parent.parent / "memorymaster" / "config_templates" / "hooks" / "memorymaster-validate-wiki.py"


def _run_hook(article_path: Path) -> tuple[int, str]:
    payload = json.dumps({"tool_input": {"file_path": str(article_path)}})
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload, capture_output=True, text=True, timeout=10,
    )
    return proc.returncode, proc.stdout


def test_validate_wiki_no_warnings_on_complete_frontmatter(tmp_path: Path) -> None:
    article = tmp_path / "obsidian-vault" / "wiki" / "project-x" / "complete.md"
    article.parent.mkdir(parents=True, exist_ok=True)
    article.write_text(
        "---\n"
        "title: Complete\n"
        "type: decision\n"
        "scope: project:x\n"
        "description: A complete article with every recommended field set, including explored.\n"
        "tags: [test, decision]\n"
        "date: 2026-05-06\n"
        "explored: false\n"
        "---\n"
        "\n"
        "# Complete\n"
        "\n"
        "This body is intentionally long enough to exceed the 300-char orphan threshold so the\n"
        "validator checks for [[a wikilink]] in the body. We add filler so total content is\n"
        "definitely past 300 characters and the orphan rule will run against this article.\n",
        encoding="utf-8",
    )
    code, out = _run_hook(article)
    assert code == 0
    assert out.strip() == ""


def test_validate_wiki_warns_when_explored_missing(tmp_path: Path) -> None:
    article = tmp_path / "obsidian-vault" / "wiki" / "project-x" / "no-explored.md"
    article.parent.mkdir(parents=True, exist_ok=True)
    article.write_text(
        "---\n"
        "title: NoExplored\n"
        "type: decision\n"
        "scope: project:x\n"
        "description: This article omits the explored field on purpose for the test.\n"
        "tags: [test]\n"
        "date: 2026-05-06\n"
        "---\n"
        "\n"
        "# NoExplored\n"
        "\n"
        "Body long enough to trip orphan check unless it has [[a wikilink]] inside.\n"
        "Adding more text to push past the 300-char threshold so the orphan rule runs\n"
        "against this article and we know the body length is sufficient for the test.\n",
        encoding="utf-8",
    )
    code, out = _run_hook(article)
    assert code == 0
    # Hook surfaces a soft warning via additionalContext.
    assert "explored" in out
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "explored" in ctx
