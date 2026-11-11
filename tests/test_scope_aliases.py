"""A folder can be declared an alias of another project's scope (operator decision 2026-10-06).

Worktrees and branch copies (whatsappbot-main-wt, pedrito-libs-20261001, ...) wrote their
memories under their own folder name and read only that, so the project's knowledge never
reached them. scope_from_cwd serves both writes and recall, so one alias fixes both.
"""
from __future__ import annotations

import json

from memorymaster.core import scope_utils
from memorymaster.core.scope_utils import scope_from_cwd


def _aliases(tmp_path, monkeypatch, payload) -> None:
    path = tmp_path / "scope-aliases.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(scope_utils.SCOPE_ALIASES_ENV, str(path))


def test_an_aliased_folder_resolves_to_its_project(tmp_path, monkeypatch) -> None:
    _aliases(tmp_path, monkeypatch, {"aliases": {"pedrito-libs-20261001": "project:pedrito"}})
    assert scope_from_cwd(tmp_path / "worktrees" / "Pedrito-Libs-20261001") == "project:pedrito"
    assert scope_from_cwd(tmp_path / "pedrito") == "project:pedrito"
    assert scope_from_cwd(tmp_path / "other") == "project:other"


def test_no_file_or_a_broken_file_changes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(scope_utils.SCOPE_ALIASES_ENV, str(tmp_path / "missing.json"))
    assert scope_from_cwd(tmp_path / "x-wt") == "project:x-wt"
    _aliases(tmp_path, monkeypatch, "{not json")
    assert scope_from_cwd(tmp_path / "x-wt") == "project:x-wt"


def test_only_project_scopes_are_accepted_as_targets(tmp_path, monkeypatch) -> None:
    _aliases(tmp_path, monkeypatch, {"aliases": {"a": "global", "b": "project:bee", "c": 3}})
    assert scope_from_cwd(tmp_path / "a") == "project:a"
    assert scope_from_cwd(tmp_path / "b") == "project:bee"
    assert scope_from_cwd(tmp_path / "c") == "project:c"


def test_an_edited_file_is_picked_up_without_a_restart(tmp_path, monkeypatch) -> None:
    import os

    _aliases(tmp_path, monkeypatch, {"aliases": {"wt": "project:one"}})
    assert scope_from_cwd(tmp_path / "wt") == "project:one"
    path = tmp_path / "scope-aliases.json"
    path.write_text(json.dumps({"aliases": {"wt": "project:two"}}), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    assert scope_from_cwd(tmp_path / "wt") == "project:two"


def test_explicit_alias_overrides_inferred_repository_and_resolves_deleted_worktree(tmp_path, monkeypatch):
    from memorymaster.core.scope_utils import resolve_transcript_workspace_scope

    _aliases(tmp_path, monkeypatch, {"aliases": {"physical-name": "project:logical", "t1664": "project:logical"}})
    repo = tmp_path / "physical-name"
    (repo / ".git").mkdir(parents=True)
    assert resolve_transcript_workspace_scope(repo) == "project:logical"
    assert resolve_transcript_workspace_scope(tmp_path / "deleted" / "t1664" / "src") == "project:logical"


def test_dreaming_capture_resolves_worktree_style_cwds_like_the_live_hooks(tmp_path, monkeypatch):
    """D-13: 504 captures landed in project:whatsappbot-main-wt, an aliased worktree folder. A copy-suffixed folder
    ("... - Copy") canonicalizes to the aliased slug, and the strict capture resolver ignored the slug alias that
    scope_from_cwd honours, so the beat filed it under the worktree's own name."""
    from memorymaster.dreaming import capture

    _aliases(tmp_path, monkeypatch, {"aliases": {"whatsappbot-main-wt": "project:whatsappbot-final"}})
    repo = tmp_path / "whatsappbot-main-wt - Copy"
    (repo / "src").mkdir(parents=True)
    gitdir = repo / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    linked = tmp_path / "wt"  # a linked worktree of that repo, as `git worktree add` lays it out
    (linked / "pkg").mkdir(parents=True)
    (gitdir / "commondir").write_text("../..", encoding="utf-8")
    (gitdir / "gitdir").write_text(str(linked / ".git"), encoding="utf-8")
    (linked / ".git").write_text(f"gitdir: {gitdir}", encoding="utf-8")
    for cwd in (repo, repo / "src", linked, linked / "pkg"):
        assert capture._scope(str(cwd)) == "project:whatsappbot-final", cwd
        assert scope_from_cwd(cwd) == capture._scope(str(cwd)), cwd


def test_strict_and_live_resolution_agree_on_a_slug_alias(tmp_path, monkeypatch):
    from memorymaster.core.scope_utils import resolve_transcript_workspace_scope

    _aliases(tmp_path, monkeypatch, {"aliases": {"whatsappbot": "project:whatsappbot-final"}})
    repo = tmp_path / "whatsappbot-prod - Copy - Copy"
    (repo / ".git").mkdir(parents=True)
    assert resolve_transcript_workspace_scope(repo) == scope_from_cwd(repo) == "project:whatsappbot-final"
    other = tmp_path / "elsewhere"
    (other / ".git").mkdir(parents=True)
    assert resolve_transcript_workspace_scope(other) == "project:elsewhere", "no alias, no change"
