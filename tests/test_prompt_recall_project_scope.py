"""Prompt recall reads the project the prompt came from (found 2026-10-05).

Writes took their scope from the session's cwd (scope_from_cwd) but prompt recall
searched a fixed `project:memorymaster` plus `global`: measured live, pubgclone,
wezbridge and pedrito prompts all received MemoryMaster's own claims and never their
own (wezbridge 121 confirmed, pedrito 105, whatsappbot thousands). That is why the
injected memories were almost never used (1 strong use in 1,066 exposures).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import context_hook
from memorymaster.recall.qdrant_outbox import ENV_OUTBOX_DIR


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    monkeypatch.setenv(ENV_OUTBOX_DIR, str(tmp_path / "outbox"))
    for name in ("QDRANT_URL", "MEMORYMASTER_SCOPE_DEFAULT", "MEMORYMASTER_JEV_MODE"):
        monkeypatch.delenv(name, raising=False)
    svc = MemoryService(tmp_path / "scope.db", workspace_root=tmp_path)
    svc.init_db()
    for scope in ("project:pubgclone", "project:memorymaster"):
        claim = svc.store.create_claim(text=f"weapon recoil spread pattern decided for {scope}",
                                       citations=[CitationInput(source="test://scope")], scope=scope)
        lifecycle.transition_claim(svc.store, claim.id, "confirmed", reason="fixture", event_type="validator")
    return str(svc.store.db_path)


def _scopes(db: str, ids: list[int]) -> set[str]:
    svc = MemoryService(db, workspace_root=Path(db).parent, read_only=True)
    return {svc.store.get_claim(i, include_citations=False).scope for i in ids}


def test_a_prompt_from_a_project_folder_reads_that_project(db, tmp_path) -> None:
    cwd = tmp_path / "pubgclone"
    cwd.mkdir()
    _ctx, ids = context_hook.recall("how does weapon recoil spread work", db_path=db, skip_qdrant=True,
                                    return_ids=True, hook_data={"cwd": str(cwd), "session_id": "s"})
    assert "project:pubgclone" in _scopes(db, ids)
    assert "project:memorymaster" not in _scopes(db, ids)


def test_without_a_folder_the_configured_default_still_applies(db) -> None:
    _ctx, ids = context_hook.recall("how does weapon recoil spread work", db_path=db, skip_qdrant=True,
                                    return_ids=True)
    assert _scopes(db, ids) == {"project:memorymaster"}


def test_the_request_scope_does_not_leak_into_the_next_call(db, tmp_path) -> None:
    cwd = tmp_path / "pubgclone"
    cwd.mkdir()
    context_hook.recall("weapon recoil", db_path=db, skip_qdrant=True, hook_data={"cwd": str(cwd)})
    assert context_hook._current_scope() == "project:memorymaster"


def test_an_explicit_scope_default_still_pins_the_scope(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_SCOPE_DEFAULT", "project:memorymaster")
    cwd = tmp_path / "pubgclone"
    cwd.mkdir()
    _ctx, ids = context_hook.recall("how does weapon recoil spread work", db_path=db, skip_qdrant=True,
                                    return_ids=True, hook_data={"cwd": str(cwd)})
    assert _scopes(db, ids) == {"project:memorymaster"}
