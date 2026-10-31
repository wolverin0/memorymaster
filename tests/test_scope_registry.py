"""Two repositories with the same folder name no longer share one scope (T-1874).

Scope used to be the folder name alone, so ``client-a/api`` and ``client-b/api``
both wrote into ``project:api``. A persistent root->scope registry, kept outside
the claims DB, now gives the first root the plain scope and a later same-named
root a derived one. It is forward-only: no stored claim is rewritten.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest

from memorymaster.core import scope_utils
from memorymaster.core.models import CitationInput
from memorymaster.core.scope_utils import resolve_transcript_workspace_scope, scope_from_cwd
from memorymaster.core.service import MemoryService


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _worktree(repo: Path, checkout: Path) -> Path:
    gitdir = repo / ".git" / "worktrees" / checkout.name
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text(f"gitdir: {os.path.relpath(gitdir, checkout)}\n", encoding="utf-8")
    return checkout


def _registry() -> dict:
    return json.loads(Path(os.environ[scope_utils.SCOPE_REGISTRY_ENV]).read_text(encoding="utf-8"))["roots"]


def test_the_collision_warning_fires_once(tmp_path, caplog) -> None:
    _repo(tmp_path / "one" / "api")
    b = _repo(tmp_path / "two" / "api")
    scope_from_cwd(tmp_path / "one" / "api")
    with caplog.at_level(logging.WARNING, logger=scope_utils.__name__):
        for _ in range(3):
            scope_from_cwd(b)
    assert len([r for r in caplog.records if "scope collision" in r.getMessage()]) == 1


def test_worktrees_resolve_to_the_owning_repository_scope(tmp_path) -> None:
    _repo(tmp_path / "client-a" / "api")
    b = _repo(tmp_path / "client-b" / "api")
    scope_from_cwd(tmp_path / "client-a" / "api")
    b_scope = scope_from_cwd(b)

    checkout = _worktree(b, tmp_path / "worktrees" / "t-1874")
    assert scope_from_cwd(checkout) == b_scope
    assert resolve_transcript_workspace_scope(checkout) == b_scope
    assert len(_registry()) == 2, "a worktree must not register a root of its own"


def test_explicit_aliases_win_and_do_not_register(tmp_path, monkeypatch) -> None:
    aliases = tmp_path / "scope-aliases.json"
    aliases.write_text(json.dumps({"aliases": {"api": "project:client-b-api"}}), encoding="utf-8")
    monkeypatch.setenv(scope_utils.SCOPE_ALIASES_ENV, str(aliases))
    b = _repo(tmp_path / "client-b" / "api")

    assert scope_from_cwd(b) == "project:client-b-api"
    assert resolve_transcript_workspace_scope(b) == "project:client-b-api"
    assert not Path(os.environ[scope_utils.SCOPE_REGISTRY_ENV]).exists()


def test_names_that_only_fold_together_still_share_the_plain_scope(tmp_path) -> None:
    # whatsappbot-final -> whatsappbot is a deliberate fold, not a collision.
    assert scope_from_cwd(_repo(tmp_path / "x" / "whatsappbot")) == "project:whatsappbot"
    assert scope_from_cwd(_repo(tmp_path / "y" / "whatsappbot-final")) == "project:whatsappbot"


@pytest.mark.parametrize("content", ["{not json", '{"roots": []}'])
def test_a_broken_registry_falls_back_to_the_plain_scope_and_is_not_overwritten(tmp_path, content) -> None:
    registry = Path(os.environ[scope_utils.SCOPE_REGISTRY_ENV])
    registry.write_text(content, encoding="utf-8")
    assert scope_from_cwd(_repo(tmp_path / "client-a" / "api")) == "project:api"
    assert registry.read_text(encoding="utf-8") == content


def test_an_unwritable_registry_falls_back_to_the_plain_scope(tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv(scope_utils.SCOPE_REGISTRY_ENV, str(blocker / "scope-registry.json"))
    assert scope_from_cwd(_repo(tmp_path / "client-a" / "api")) == "project:api"


def test_existing_claims_are_not_rewritten(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    db = tmp_path / "claims.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    svc.store.create_claim(
        text="the api service listens behind the gateway", subject="api", predicate="topology",
        object_value="behind gateway", confidence=0.8, scope="project:api", source_agent="test",
        citations=[CitationInput(source="session://x", locator="turn:1", excerpt="gateway")],
    )

    def rows() -> list[tuple]:
        with sqlite3.connect(db) as conn:
            return conn.execute("SELECT * FROM claims ORDER BY id").fetchall()

    before = rows()
    scope_from_cwd(_repo(tmp_path / "client-a" / "api"))
    assert scope_from_cwd(_repo(tmp_path / "client-b" / "api")) != "project:api"
    assert rows() == before


def test_the_collided_repo_does_not_read_the_plain_scope_it_lost(tmp_path, monkeypatch) -> None:
    from memorymaster.surfaces import mcp_server as m

    monkeypatch.delenv("MEMORYMASTER_MCP_AUTH_MODE", raising=False)
    monkeypatch.delenv("MEMORYMASTER_SCOPE_DISAMBIGUATE", raising=False)
    monkeypatch.setattr(m, "_ENV_DEFAULT_PROJECT_SCOPE", "")
    monkeypatch.setattr(m, "_ENV_RECALL_ANCESTOR_SCOPES", True)
    a = _repo(tmp_path / "client-a" / "api")
    b = _repo(tmp_path / "client-b" / "api")

    assert "project:api" in m._effective_scope_allowlist("", str(a))
    b_allow = m._effective_scope_allowlist("", str(b))
    assert b_allow[0] == scope_from_cwd(b) != "project:api"
    assert "project:api" not in b_allow, "client-b would read client-a's claims"
