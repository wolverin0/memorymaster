"""ROADMAP item 6: a replica archives tenant-less twins of claims its origin stamped with a tenant.

Hermes, 2026-10-03: 17,187 tenant-less claims had a 'personal' twin inserted by the
merge after Windows stamped the tenant; Hermes recall returned both copies.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.bridges import tenant_twins
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import qdrant_outbox

TEXT = "The shared MCP server listens on the loopback port 8766."


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv(qdrant_outbox.ENV_OUTBOX_DIR, str(tmp_path / "outbox"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    path = tmp_path / "replica.db"
    MemoryService(path, workspace_root=tmp_path).init_db()
    return path


def _claim(db: Path, text: str, *, tenant: str | None, scope: str = "project:mm") -> int:
    svc = MemoryService(db, workspace_root=db.parent, tenant_id=tenant)
    return svc.ingest(text, [CitationInput(source="test://twins")], scope=scope).id


def _row(db: Path, claim_id: int):
    return MemoryService(db, workspace_root=db.parent).store.get_claim(claim_id, include_citations=False)


def test_only_the_tenant_less_twin_is_archived_and_points_at_its_twin(db: Path) -> None:
    old = _claim(db, TEXT, tenant=None)
    twin = _claim(db, TEXT, tenant="personal")
    alone = _claim(db, "A replica-only fact with no stamped twin.", tenant=None)
    other_scope = _claim(db, TEXT, tenant=None, scope="project:other")
    store = MemoryService(db, workspace_root=db.parent).store

    assert tenant_twins.archive_twins(store, db, "personal") == {"tenant": "personal", "twins": 1, "archived": 0, "failed": {}}
    assert _row(db, old).status != "archived"  # dry run changes nothing

    assert tenant_twins.archive_twins(store, db, "personal", apply=True)["archived"] == 1
    archived = _row(db, old)
    assert (archived.status, archived.replaced_by_claim_id) == ("archived", twin)
    assert {_row(db, i).status for i in (twin, alone, other_scope)} == {"candidate"}
    assert tenant_twins.archive_twins(store, db, "personal", apply=True)["twins"] == 0


def test_another_tenant_is_never_treated_as_a_twin(db: Path) -> None:
    _claim(db, TEXT, tenant=None)
    _claim(db, TEXT, tenant="team:other")
    store = MemoryService(db, workspace_root=db.parent).store
    assert tenant_twins.archive_twins(store, db, "personal", apply=True)["twins"] == 0
