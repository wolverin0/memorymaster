"""El gate de tenant debe decir la verdad: la claim EXISTE, la sesion no la alcanza.

Deuda (b) del handoff 2026-08-26: desde una sesion MCP con tenant 'personal' no
se puede escribir sobre el corpus historico (tenant NULL), y el error decia
"Claim N does not exist" — desinformacion que costo diagnosticos enteros, porque
la claim SI existe y get_claim ya la habia devuelto (no hay ocultamiento que
proteger: esto es distinto del caso RLS multi-usuario, donde la fila viene
filtrada ANTES y el "does not exist" es no-leaking a proposito).

Contrato alineado con el guard Postgres de la migracion 0012, cuyo test
(test_v0012_installs_nonleaking_complete_boundary_guard) exige "outside the
authorized boundary" y prohibe "does not exist" en el guard.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.service import MemoryService


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "gate.db")


def test_cross_tenant_write_names_the_boundary_not_a_lie(db_path, tmp_path):
    from memorymaster.core.models import CitationInput

    untenanted = MemoryService(db_path, workspace_root=tmp_path)
    untenanted.init_db()
    claim = untenanted.ingest(
        "Historic fact from the NULL-tenant corpus.",
        [CitationInput(source="test://tenant-gate")],
        scope="project:test",
        source_agent="test",
    )

    scoped = MemoryService(db_path, workspace_root=tmp_path, tenant_id="personal")
    with pytest.raises(ValueError) as excinfo:
        scoped.pin(claim.id)
    message = str(excinfo.value)
    assert "does not exist" not in message, "el gate no puede negar una claim visible"
    assert "tenant" in message.lower()
    assert str(claim.id) in message


def test_truly_missing_claim_still_says_does_not_exist(db_path, tmp_path):
    svc = MemoryService(db_path, workspace_root=tmp_path, tenant_id="personal")
    svc.init_db()
    with pytest.raises(ValueError, match="does not exist"):
        svc.pin(999999)
