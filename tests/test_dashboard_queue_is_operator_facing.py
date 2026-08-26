"""La cola del DASHBOARD muestra solo lo del operador o lo flaggeado.

TERCERA ITERACION DE LA MISMA QUEJA (2026-08-26). Tras drenar conflictos y
propuestas, el operador abrio #38562 ("mzcopilot Burn #3", contabilidad de
mayo, stale, project-scope) desde la cola del dashboard y dijo "this is really
really bad". Tenia razon: stale es DECAIMIENTO — lo gestionan el drain, el
decay y el archivador — y mostrarselo a un humano es pedirle que arbitre
basura operativa. Medido: 27.983 stale, de los cuales 25.291 son de maquina.

LA SEPARACION DE SUPERFICIES ES EL PUNTO: `build_review_queue` (la libreria y
el CLI review-queue) CONSERVA la vista completa, porque el jardinero y los
agentes la necesitan. El filtro vive SOLO en el read model del dashboard
(`_operator_facing`): pertenece a la vista humana lo que es del operador
(scope=user o pinned) o lo que trae propuesta pendiente.

`test_operator_items_and_flagged_stay` es el control positivo: sin el, un
filtro que devuelve lista vacia pasaria el resto del archivo.
"""
from __future__ import annotations

from memorymaster.surfaces.dashboard_read_models import _operator_facing


def _item(claim_id: int, *, scope: str = "project:x", pinned: bool = False) -> dict:
    return {"claim_id": claim_id, "scope": scope, "pinned": pinned, "status": "stale"}


def test_machine_stale_never_reaches_the_human_queue():
    """El caso exacto de la queja: #38562, stale de project-scope, sin propuesta."""
    items = [_item(38562, scope="project:mzcopilot"), _item(12964, scope="project:memorymaster")]

    assert _operator_facing(items, proposals={}) == []


def test_operator_items_and_flagged_stay():
    """scope=user NO retiene: esa etiqueta la pone el extractor, no el humano.

    Refutado por el operador el mismo dia: 70 de sus 72 items user-scope eran
    de atlas-llm-extractor, con duplicados literales.
    """
    items = [
        _item(1, scope="user"),                      # etiqueta de maquina -> fuera
        _item(2, pinned=True),                       # acto humano -> queda
        _item(3, scope="project:x"),                 # de maquina CON propuesta -> queda
        _item(4, scope="project:y"),                 # de maquina sin nada -> fuera
    ]

    kept = _operator_facing(items, proposals={3: {"proposal_event_id": 9}})

    assert [i["claim_id"] for i in kept] == [2, 3]


def test_an_item_without_ownership_fields_is_not_kept_by_accident():
    """Items viejos sin scope/pinned (cache, versiones previas) no pasan por defecto."""
    assert _operator_facing([{"claim_id": 5}], proposals={}) == []
