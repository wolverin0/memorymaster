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
