"""Una propuesta que se cumplio sola no puede seguir contando como pendiente.

EL DEFECTO. El contador de la cola del steward solo SUMABA. Cuando el decay, la
dedup o una supersesion posterior dejaban la claim en el estado que la propuesta
pedia, nadie restaba esa propuesta: seguia "pendiente" para siempre. Al
2026-08-20 la cola declaraba 254 pendientes y las decisiones reales eran 45.

Un numero falso HACIA ARRIBA es peor que no tener numero. Manda a buscar trabajo
donde no hay, y entierra las 45 decisiones que importan debajo de 209 que no. El
operador lo dijo de la forma mas clara posible: "no entiendo que tengo que hacer".

EL CONTRA-CASO IMPORTA IGUAL O MAS. Un filtro que esconde propuestas es
exactamente el riesgo de este arreglo: pasar de un numero inflado a uno que oculta
decisiones reales seria peor, porque el primero al menos se nota.
`test_a_live_decision_stays_pending` y `test_a_lookup_failure_leaves_it_pending`
son los que impiden eso.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.core.lifecycle import transition_claim
from memorymaster.govern.steward import list_steward_proposals


@pytest.fixture()
def svc(tmp_path: Path) -> MemoryService:
    s = MemoryService(tmp_path / "queue.db", workspace_root=tmp_path)
    s.init_db()
    return s


def _claim(svc: MemoryService, text: str, *, confirm: bool = False):
    """Ingiere una claim. `confirm=True` la lleva a confirmed, que es el estado
    desde el que el ciclo de vida permite decaer o supersedir — una claim recien
    ingerida nace `candidate` y no admite esas transiciones."""
    claim = svc.ingest(
        text=text,
        citations=[CitationInput(source="test", locator="l", excerpt="e")],
        scope="project:test", source_agent="pytest", confidence=0.8,
    )
    if confirm:
        transition_claim(svc.store, claim_id=claim.id, to_status="confirmed",
                         reason="test setup", event_type="transition")
    return claim


def _propose(svc: MemoryService, claim_id: int, proposed_status: str, decision: str) -> None:
    """Registra una propuesta del steward tal como la escribe el pipeline real."""
    svc.store.record_event(
        claim_id=claim_id,
        event_type="policy_decision",
        details=f"steward_proposal:{decision}",
        payload={"decision": decision, "proposed_status": proposed_status, "priority": 0.9},
    )


def _pending_ids(svc: MemoryService) -> set[int]:
    return {p["claim_id"] for p in list_steward_proposals(svc, limit=500, include_resolved=False)}


def test_a_proposal_whose_outcome_already_happened_leaves_the_queue(svc):
    """El caso de las 209: la claim ya esta donde la propuesta queria dejarla."""
    claim = _claim(svc, "Backups run weekly and are trimmed on the E drive.", confirm=True)
    _propose(svc, claim.id, proposed_status="stale", decision="stale")
    assert claim.id in _pending_ids(svc), "precondicion: nace pendiente"

    transition_claim(svc.store, claim_id=claim.id, to_status="stale",
                     reason="decay", event_type="transition")

    assert claim.id not in _pending_ids(svc), (
        "la claim ya esta stale, que es lo que la propuesta pedia: no hay nada "
        "que decidir y no puede seguir contando como pendiente"
    )


def test_an_archived_claim_settles_any_proposal(svc):
    """Archivada es terminal: ninguna propuesta sobrevive a eso."""
    claim = _claim(svc, "Temporary note about a deployment that no longer exists.", confirm=True)
    _propose(svc, claim.id, proposed_status="superseded", decision="superseded_candidate")

    transition_claim(svc.store, claim_id=claim.id, to_status="archived",
                     reason="compaction", event_type="transition")

    assert claim.id not in _pending_ids(svc)


def test_a_live_decision_stays_pending(svc):
    """CONTRA-METRICA: no se gana vaciando la cola.

    Una propuesta sobre una claim VIVA es una decision real y tiene que seguir
    ahi. Si este test se rompe, el arreglo paso de inflar el numero a esconder
    trabajo, que es peor: lo inflado se nota y lo escondido no.
    """
    claim = _claim(svc, "The steward promotes candidate claims once corroborated.")
    _propose(svc, claim.id, proposed_status="superseded", decision="superseded_candidate")

    assert claim.id in _pending_ids(svc), (
        "una supersesion propuesta sobre una claim confirmada exige juicio "
        "humano y no puede desaparecer de la cola"
    )


def test_a_lookup_failure_leaves_it_pending(svc, monkeypatch):
    """Ante la duda, pendiente. Un error al leer no puede cerrar una decision."""
    claim = _claim(svc, "Recall ranking orders results by lexical relevance.")
    _propose(svc, claim.id, proposed_status="superseded", decision="superseded_candidate")

    def explota(*a, **k):
        raise RuntimeError("store caido")

    monkeypatch.setattr(svc.store, "get_claim", explota)

    assert claim.id in _pending_ids(svc), (
        "un fallo de lectura se resolvio como 'saldada' y escondio una decision"
    )
