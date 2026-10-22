"""Tests for v3.9.0 F3 — cwd-from-transcript scope derivation (MemPalace v3.3.3 pattern)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memorymaster.core.scope_utils import (
    cwd_from_transcript,
    scope_from_cwd,
    scope_from_transcript,
    resolve_transcript_workspace_scope,
    project_scope_variants,
)


def test_scope_from_cwd_basic(tmp_path):
    assert scope_from_cwd(tmp_path / "memorymaster") == "project:memorymaster"


def test_scope_from_cwd_lowercases_and_dashes(tmp_path):
    assert scope_from_cwd(tmp_path / "My Project Name") == "project:my-project-name"


def test_scope_from_cwd_returns_global_for_empty():
    assert scope_from_cwd(None) == "global"
    assert scope_from_cwd("") == "global"


def test_scope_from_cwd_returns_global_for_nameless_root():
    """Root-only paths like ``/`` have empty .name → fall back to global."""
    assert scope_from_cwd("/") == "global"


def test_cwd_from_transcript_first_record(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"cwd": "/path/to/myproject", "role": "user"}) + "\n",
        encoding="utf-8",
    )
    assert cwd_from_transcript(transcript) == "/path/to/myproject"


def test_cwd_from_transcript_skips_records_without_cwd(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"role": "system"}),
                "",  # blank line
                "not-json-garbage",
                json.dumps({"role": "user"}),  # no cwd
                json.dumps({"cwd": "/found/it", "role": "assistant"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert cwd_from_transcript(transcript) == "/found/it"


def test_cwd_from_transcript_missing_file(tmp_path):
    assert cwd_from_transcript(tmp_path / "does-not-exist.jsonl") is None


def test_cwd_from_transcript_none_arg():
    assert cwd_from_transcript(None) is None


def test_cwd_from_transcript_empty_file(tmp_path):
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert cwd_from_transcript(transcript) is None


def test_cwd_from_transcript_no_cwd_anywhere(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"role": "user"}) + "\n" + json.dumps({"role": "assistant"}) + "\n",
        encoding="utf-8",
    )
    assert cwd_from_transcript(transcript) is None


def test_scope_from_transcript_uses_transcript_first(tmp_path):
    """When the transcript HAS a cwd, the fallback_cwd is ignored."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"cwd": "/path/to/authoritative", "role": "user"}) + "\n",
        encoding="utf-8",
    )
    out = scope_from_transcript(transcript, fallback_cwd="/wrong/path")
    assert out == "project:authoritative"


def test_scope_from_transcript_falls_back_to_arg(tmp_path):
    """When the transcript has no cwd, the fallback_cwd wins."""
    transcript = tmp_path / "no-cwd.jsonl"
    transcript.write_text(json.dumps({"role": "user"}) + "\n", encoding="utf-8")
    out = scope_from_transcript(transcript, fallback_cwd="/fallback/myapp")
    assert out == "project:myapp"


def test_scope_from_transcript_falls_back_to_global(tmp_path):
    transcript = tmp_path / "no-cwd.jsonl"
    transcript.write_text(json.dumps({"role": "user"}) + "\n", encoding="utf-8")
    assert scope_from_transcript(transcript, fallback_cwd=None) == "global"


def test_scope_from_transcript_handles_missing_transcript(tmp_path):
    """Missing file → None from cwd_from_transcript → fallback path used."""
    out = scope_from_transcript(tmp_path / "missing.jsonl", fallback_cwd="/x/myapp")
    assert out == "project:myapp"


def _worktree(repo, directory):
    gitdir = repo / ".git" / "worktrees" / directory.name
    gitdir.mkdir(parents=True)
    directory.mkdir(parents=True)
    (directory / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (gitdir / "gitdir").write_text(str(directory / ".git"), encoding="utf-8")
    return directory


def test_repository_identity_is_shared_by_card_pr_and_canonical_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(tmp_path / "absent.json"))
    repo = tmp_path / "MemoryMaster-final"
    (repo / ".git").mkdir(parents=True)
    paths = [repo, _worktree(repo, repo / "_wt" / "t1664"), _worktree(repo, tmp_path / "pr-123")]
    for path in paths:
        nested = path / "src" / "module"
        nested.mkdir(parents=True)
        assert resolve_transcript_workspace_scope(nested) == "project:memorymaster"
        assert scope_from_cwd(nested) == "project:memorymaster"
    assert project_scope_variants(repo.name) == ["project:memorymaster", "project:memorymaster-final"]


def test_unverified_deleted_or_scratch_workspaces_are_quarantined(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(tmp_path / "absent.json"))
    (tmp_path / ".git").mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert resolve_transcript_workspace_scope(scratch) is None
    assert resolve_transcript_workspace_scope(tmp_path / "_wt" / "t1664") is None
    assert resolve_transcript_workspace_scope(None) is None
    assert resolve_transcript_workspace_scope(None, allow_fallback_global=True) == "global"


def test_worktree_pointer_requires_valid_common_membership_and_backlink(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(tmp_path / "absent.json"))
    repo = tmp_path / "repo"
    wt = _worktree(repo, tmp_path / "pr-123")
    gitdir = repo / ".git" / "worktrees" / wt.name
    (gitdir / "commondir").write_text("../../../../unrelated", encoding="utf-8")
    assert resolve_transcript_workspace_scope(wt) is None
    (gitdir / "commondir").write_text("../..", encoding="utf-8")
    (gitdir / "gitdir").write_text(str(tmp_path / "another" / ".git"), encoding="utf-8")
    assert resolve_transcript_workspace_scope(wt) is None


@pytest.fixture(params=[
    "relative", "absolute", "broken_gitfile", "missing_gitdir", "empty_commondir",
    "missing_commondir", "wrong_membership", "wrong_backlink", "plain", "deleted",
    "nameless", "owner_alias", "checkout_alias", "deleted_alias",
])
def scope_mode_workspace(request, tmp_path, monkeypatch):
    """Both modes see exactly the same disposable metadata and alias tree."""
    repo = tmp_path / "MemoryMaster-final"
    checkout = _worktree(repo, tmp_path / "review-checkout")
    nested = checkout / "src"
    nested.mkdir()
    gitdir = repo / ".git" / "worktrees" / checkout.name
    (checkout / ".git").write_text(f"gitdir: {os.path.relpath(gitdir, checkout)}\n", encoding="utf-8")
    aliases = tmp_path / "scope-aliases.json"
    aliases.write_text(json.dumps({"aliases": {}}), encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(aliases))
    case = request.param
    cwd, relaxed, strict = nested, "project:memorymaster", "project:memorymaster"
    if case == "absolute":
        (checkout / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        (gitdir / "commondir").write_text(str(repo / ".git"), encoding="utf-8")
    elif case in {"broken_gitfile", "missing_gitdir"}:
        (checkout / ".git").write_text(
            "nonsense" if case == "broken_gitfile" else "gitdir: missing", encoding="utf-8",
        )
        relaxed, strict = "project:review-checkout", None
    elif case in {"empty_commondir", "missing_commondir"}:
        (gitdir / "commondir").write_text(
            "" if case == "empty_commondir" else "missing", encoding="utf-8",
        )
        relaxed, strict = "project:review-checkout", None
    elif case == "wrong_membership":
        unrelated = tmp_path / "unrelated" / ".git"
        unrelated.mkdir(parents=True)
        (gitdir / "commondir").write_text(str(unrelated), encoding="utf-8")
        relaxed, strict = "project:unrelated", None
    elif case == "wrong_backlink":
        (gitdir / "gitdir").write_text(str(tmp_path / "other" / ".git"), encoding="utf-8")
        strict = None
    elif case in {"plain", "deleted", "deleted_alias"}:
        cwd = tmp_path / "scratch" / "plain-project"
        if case == "plain":
            cwd.mkdir(parents=True)
        relaxed, strict = "project:plain-project", None
        if case == "deleted_alias":
            aliases.write_text(json.dumps({"aliases": {"plain-project": "project:logical"}}), encoding="utf-8")
            relaxed = strict = "project:logical"
    elif case == "nameless":
        cwd, relaxed, strict = Path(tmp_path.anchor), "global", None
        # Exercise a nameless root without inspecting the host filesystem.
        for method in ("is_dir", "is_file", "exists"):
            original = getattr(Path, method)
            monkeypatch.setattr(Path, method, lambda path, original=original: (
                False if path in {cwd, cwd / ".git"} else original(path)
            ))
    elif case in {"owner_alias", "checkout_alias"}:
        name = repo.name.lower() if case == "owner_alias" else checkout.name
        aliases.write_text(json.dumps({"aliases": {name: "project:logical"}}), encoding="utf-8")
        relaxed = strict = "project:logical"
    return cwd, relaxed, strict


@pytest.mark.parametrize("strict", [False, True], ids=["relaxed", "strict"])
def test_workspace_scope_explicit_modes_match_public_wrappers(scope_mode_workspace, strict):
    from memorymaster.core.scope_utils import resolve_workspace_scope

    cwd, relaxed_scope, strict_scope = scope_mode_workspace
    expected = strict_scope if strict else relaxed_scope
    assert resolve_workspace_scope(cwd, strict=strict) == expected
    wrapper = resolve_transcript_workspace_scope if strict else scope_from_cwd
    assert wrapper(cwd) == expected


@pytest.mark.parametrize("strict", [False, True], ids=["relaxed", "strict"])
def test_workspace_scope_modes_preserve_unresolvable_path_policy(tmp_path, monkeypatch, strict):
    from memorymaster.core.scope_utils import resolve_workspace_scope

    cwd = tmp_path / "loop"
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == cwd:
            raise RuntimeError("symlink loop")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    if strict:
        assert resolve_workspace_scope(cwd, strict=True) is None
    else:
        with pytest.raises(RuntimeError, match="symlink loop"):
            resolve_workspace_scope(cwd, strict=False)


@pytest.mark.parametrize("name", ["My_Project", "My.Project", "t_12", "My Project"])
@pytest.mark.parametrize("location", ["plain", "deleted", "ancestor", "repository", "checkout", "owner"])
def test_strict_workspace_scope_normalizes_aliases(tmp_path, monkeypatch, name, location):
    from memorymaster.core.scope_utils import resolve_workspace_scope

    # Alias normalization folds whitespace only; punctuation stays literal.
    alias_key = "-".join(name.strip().lower().split())
    aliases = tmp_path / "scope-aliases.json"
    aliases.write_text(json.dumps({"aliases": {alias_key: "project:logical"}}), encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(aliases))
    cwd = tmp_path / name
    if location == "checkout":
        cwd = _worktree(tmp_path / "owner", cwd)
    elif location == "owner":
        cwd = _worktree(cwd, tmp_path / "checkout")
    elif location != "deleted":
        cwd.mkdir()
        if location == "repository":
            (cwd / ".git").mkdir()
        elif location == "ancestor":
            cwd = cwd / "src"
            cwd.mkdir()

    assert resolve_workspace_scope(cwd, strict=True) == "project:logical"
    assert resolve_transcript_workspace_scope(cwd) == "project:logical"
