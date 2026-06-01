"""Regression: capture_query_result must namespace files by scope.

WHY: two queries with identical query_text but different scopes (e.g.
project:foo vs project:bar) used to slugify to the same filename and silently
overwrite each other, destroying one scope's captured synthesis. A dead
scope-slug expression showed a per-scope subdir was the original intent. These
tests anchor on the requirement: same query_text + different scope => distinct,
co-existing files.
"""
from __future__ import annotations

from pathlib import Path

from memorymaster.vault_query_capture import capture_query_result


def _hi_conf_claims() -> list[dict]:
    return [
        {"id": 1, "confidence": 0.9, "text": "answer a", "subject": "x"},
        {"id": 2, "confidence": 0.9, "text": "answer b", "subject": "y"},
    ]


def test_same_query_different_scope_does_not_overwrite(tmp_path: Path):
    claims = _hi_conf_claims()
    r1 = capture_query_result("how does auth work", claims, tmp_path, scope="project:foo")
    r2 = capture_query_result("how does auth work", claims, tmp_path, scope="project:bar")

    assert r1["captured"] and r2["captured"], (r1, r2)
    assert r1["file"] != r2["file"], (r1, r2)
    assert Path(r1["file"]).exists() and Path(r2["file"]).exists()


def test_scope_slug_appears_in_path(tmp_path: Path):
    r = capture_query_result(
        "topic", _hi_conf_claims(), tmp_path, scope="project:foo"
    )
    assert "project-foo" in Path(r["file"]).parts, r["file"]
