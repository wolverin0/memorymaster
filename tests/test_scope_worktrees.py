"""Git identity, rather than disposable checkout names, owns project scope."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

from memorymaster.core.access_control import AuthMode, RequestContext, Role, bind_request_context
from memorymaster.core.scope_utils import scope_from_cwd, scope_from_transcript
from memorymaster.core.session_scope import SessionScopeResolver
from memorymaster.dreaming.capture import _scope, capture_transcript
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.surfaces import mcp_server
from memorymaster.workflow_intelligence.adapters import infer_scope


# Adding a new checkout convention here must never require a production alias.
# The arbitrary name guards against fixing only today's known naming patterns.
CHECKOUTS = (
    "memorymaster/_wt/t1661",
    "home/.claude/worktrees/quiet-otter",
    "memorymaster-pr279",
    "t-card",
    "t-1661",
    "T-1661",
    "future-checkout-convention-xyz",
)


def _worktree(tmp_path: Path, layout: str, *, relative: bool = True) -> tuple[Path, Path]:
    repo = tmp_path / "memorymaster"
    gitdir = repo / ".git" / "worktrees" / "slot"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    checkout = tmp_path / layout
    checkout.mkdir(parents=True)
    pointer = os.path.relpath(gitdir, checkout) if relative else str(gitdir)
    (checkout / ".git").write_text(f"gitdir: {pointer}\n", encoding="utf-8")
    return repo, checkout


@pytest.mark.parametrize("layout", CHECKOUTS)
@pytest.mark.parametrize("relative", [True, False])
def test_worktree_common_dir_owns_scope(tmp_path, monkeypatch, layout, relative):
    repo, checkout = _worktree(tmp_path, layout, relative=relative)
    nested = checkout / "src" / "component"
    nested.mkdir(parents=True)
    monkeypatch.setenv("MEMORYMASTER_SCOPE_DISAMBIGUATE", "0")
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_PROJECT_SCOPE", "")

    for cwd in (repo, checkout, nested):
        assert scope_from_cwd(cwd) == "project:memorymaster"
        assert _scope(str(cwd)) == "project:memorymaster"
        assert infer_scope(cwd) == "project:memorymaster"
        assert mcp_server._project_scope(str(cwd)) == "project:memorymaster"
        assert SessionScopeResolver._workspace_or_user(cwd) == ("project:memorymaster", "verified_workspace")


@pytest.mark.parametrize("layout", CHECKOUTS)
def test_alias_coverage_does_not_need_single_use_checkout_entries(tmp_path, monkeypatch, layout):
    """New one-off names must inherit the project alias, never emit new scopes."""
    _, checkout = _worktree(tmp_path, layout)
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"aliases": {"memorymaster": "project:governed-memory"}}), encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(aliases))
    assert scope_from_cwd(checkout) == "project:governed-memory"
    assert _scope(str(checkout)) == "project:governed-memory"


def test_explicit_checkout_alias_still_overrides_git_identity(tmp_path, monkeypatch):
    _, checkout = _worktree(tmp_path, "memorymaster-pr279")
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"aliases": {
        "memorymaster-pr279": "project:review-sandbox", "memorymaster": "project:governed-memory",
    }}), encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(aliases))
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_PROJECT_SCOPE", "")
    assert scope_from_cwd(checkout) == "project:review-sandbox"
    assert mcp_server._project_scope(str(checkout)) == "project:review-sandbox"
    nested = checkout / "src"
    nested.mkdir()
    assert scope_from_cwd(nested) == "project:review-sandbox"
    assert mcp_server._project_scope(str(nested)) == "project:review-sandbox"


def test_capture_and_transcript_persist_parent_scope(tmp_path):
    _, checkout = _worktree(tmp_path, "memorymaster/_wt/t1661")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({
        "cwd": str(checkout), "message": {"role": "user", "content": "Keep cited memory scoped to its project."},
    }) + "\n", encoding="utf-8")
    ledger = DreamLedger(tmp_path / "capture.db")
    result = capture_transcript(
        transcript, provider="claude", session_id="worktree-session", cwd=str(checkout), ledger=ledger,
    )
    assert result["queued"] == 1
    assert ledger.get_capture(result["capture_id"])["scope"] == "project:memorymaster"
    assert scope_from_transcript(transcript) == "project:memorymaster"


def test_gitfiles_without_commondir_keep_submodule_identity(tmp_path):
    repo = tmp_path / "parent"
    metadata = repo / ".git" / "modules" / "submodule"
    metadata.mkdir(parents=True)
    submodule = repo / "child"
    submodule.mkdir()
    (submodule / ".git").write_text(f"gitdir: {metadata}\n", encoding="utf-8")
    assert scope_from_cwd(submodule) == "project:child"


@pytest.mark.parametrize("pointer", ["nonsense", "gitdir:", "gitdir: missing"])
def test_broken_gitfiles_fall_back_without_crossing_to_parent_repo(tmp_path, pointer):
    repo = tmp_path / "parent"
    (repo / ".git").mkdir(parents=True)
    checkout = repo / "separate-project"
    checkout.mkdir()
    (checkout / ".git").write_text(pointer, encoding="utf-8")
    assert scope_from_cwd(checkout) == "project:separate-project"
    # A subdirectory of that broken checkout still belongs to the checkout, not to its own name.
    nested = checkout / "src" / "pkg"
    nested.mkdir(parents=True)
    assert scope_from_cwd(nested) == "project:separate-project"


def test_plain_non_git_project_is_unchanged(tmp_path):
    project = tmp_path / "My Project"
    project.mkdir()
    assert scope_from_cwd(project) == "project:my-project"


def test_git_discovery_never_climbs_above_a_ceiling_cwd(tmp_path, monkeypatch):
    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    ceiling = outer / "isolated"
    ceiling.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(ceiling))
    assert scope_from_cwd(ceiling) == "project:isolated"
    assert scope_from_cwd(ceiling / "project") == "project:project"


def test_git_ceiling_still_checks_current_worktree_metadata(tmp_path, monkeypatch):
    _, checkout = _worktree(tmp_path, "T-1661")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(checkout))
    assert scope_from_cwd(checkout) == "project:memorymaster"


def test_repository_with_nested_repositories_keeps_its_own_scope(tmp_path):
    repo = tmp_path / "parent"
    for path in (repo, repo / "alpha", repo / "beta"):
        (path / ".git").mkdir(parents=True)
    assert scope_from_cwd(repo) == "project:parent"
    assert SessionScopeResolver._workspace_or_user(repo) == ("project:parent", "verified_workspace")
    assert scope_from_cwd(repo / "alpha") == "project:alpha"


def test_collection_root_never_implicitly_binds_global_scope(tmp_path, monkeypatch):
    collection = tmp_path / "collection"
    for name in ("alpha", "beta"):
        (collection / name / ".git").mkdir(parents=True)
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_PROJECT_SCOPE", "")
    assert scope_from_cwd(collection) == _scope(str(collection)) == "global"
    assert SessionScopeResolver._workspace_or_user(collection) == ("user", "default_user")
    assert mcp_server._project_scope(str(collection)) == "user"


def test_resolving_a_worktree_does_not_launch_git(tmp_path, monkeypatch):
    _, checkout = _worktree(tmp_path, "T-1661")

    def forbidden(*args, **kwargs):
        pytest.fail("scope resolution must not spawn Git for a readable gitfile")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert scope_from_cwd(checkout) == "project:memorymaster"


def test_worktree_default_does_not_bypass_team_scope_grants(tmp_path, monkeypatch):
    _, checkout = _worktree(tmp_path, "T-1661")
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_PROJECT_SCOPE", "")
    context = RequestContext(
        mode=AuthMode.TEAM, principal="fixture", role=Role.WRITER, tenant_id="fixture-tenant",
        workspace=str(checkout), allowed_scopes=frozenset({"project:other"}),
        allow_sensitive=False, db_target=str(tmp_path / "unopened.db"),
    )
    with bind_request_context(context):
        with pytest.raises(PermissionError, match="outside the authenticated scope grant"):
            mcp_server._effective_ingest_scope("project", str(checkout))
        assert mcp_server._effective_scope_allowlist("", str(checkout)) == ["project:other"]


def test_real_git_worktree_resolves_to_parent(tmp_path):
    repo = tmp_path / "memorymaster"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True, capture_output=True)
    subprocess.run([
        "git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "fixture",
    ], check=True, capture_output=True)
    checkout = tmp_path / "memorymaster-pr279"
    subprocess.run([
        "git", "-C", str(repo), "worktree", "add", "--quiet", "--detach", str(checkout),
    ], check=True, capture_output=True)
    assert scope_from_cwd(repo) == scope_from_cwd(checkout) == "project:memorymaster"


def test_absolute_commondir_pointer_is_relative_to_neither_checkout_nor_cwd(tmp_path):
    repo, checkout = _worktree(tmp_path, "memorymaster-pr279")
    (repo / ".git/worktrees/slot/commondir").write_text(str(repo / ".git"), encoding="utf-8")
    assert scope_from_cwd(checkout) == "project:memorymaster"


@pytest.mark.parametrize("pointer", ["", "missing-common-dir"])
def test_invalid_commondir_falls_back_to_checkout_scope(tmp_path, pointer):
    repo, checkout = _worktree(tmp_path, "separate-project")
    (repo / ".git/worktrees/slot/commondir").write_text(pointer, encoding="utf-8")
    assert scope_from_cwd(checkout) == "project:separate-project"
    nested = checkout / "docs"
    nested.mkdir()
    assert scope_from_cwd(nested) == "project:separate-project"


def test_hooks_translate_the_global_sentinel_to_user(tmp_path, monkeypatch):
    from memorymaster.core.scope_utils import scope_from_cwd_or_user

    collection = tmp_path / "apps"
    for name in ("one", "two"):
        (collection / name / ".git").mkdir(parents=True)
    assert scope_from_cwd(collection) == "global"
    assert scope_from_cwd_or_user(collection) == "user"
    assert scope_from_cwd_or_user("") == "user"
    assert scope_from_cwd_or_user(None) == "user"
    _, checkout = _worktree(tmp_path, "memorymaster-pr300")
    assert scope_from_cwd_or_user(checkout) == "project:memorymaster"
    hook = Path(__file__).resolve().parents[1] / "memorymaster/config_templates/hooks/memorymaster-session-start.py"
    spec = importlib.util.spec_from_file_location("collection_session_start", hook)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(collection)
    assert module._derive_scope() == "user"


def test_mcp_disambiguation_digest_is_shared_by_worktrees(tmp_path, monkeypatch):
    from memorymaster.surfaces import mcp_server

    monkeypatch.setenv("MEMORYMASTER_SCOPE_DISAMBIGUATE", "1")
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_PROJECT_SCOPE", "", raising=False)
    monkeypatch.setattr(mcp_server.mcp_path_policy, "validate_workspace_path", lambda *_a, **_k: None)
    repo, checkout = _worktree(tmp_path, "memorymaster-pr301")
    main_scope = mcp_server._project_scope(str(repo))
    assert main_scope.startswith("project:memorymaster:") and len(main_scope.rsplit(":", 1)[1]) == 8
    assert mcp_server._project_scope(str(checkout)) == main_scope
    assert mcp_server._project_scope(str(checkout / "sub")) == main_scope


def test_session_start_hook_uses_shared_worktree_scope(tmp_path, monkeypatch):
    _, checkout = _worktree(tmp_path, "home/.claude/worktrees/quiet-otter")
    hook = Path(__file__).resolve().parents[1] / "memorymaster/config_templates/hooks/memorymaster-session-start.py"
    spec = importlib.util.spec_from_file_location("worktree_session_start", hook)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(checkout)
    assert module._derive_scope() == "project:memorymaster"
