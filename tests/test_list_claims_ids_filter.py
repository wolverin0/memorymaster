"""Traer claims por id tiene que traer ESAS claims, o ninguna.

EL BUG QUE ORIGINA ESTE ARCHIVO. Hasta el 2026-08-21 no existia forma de pedir
claims por id via MCP. La superficie no rechaza parametros desconocidos, asi que
un llamador que pedia `ids=[133012, 133013]` —lo primero que se intenta cuando ya
se tiene el id— recibia el listado SIN FILTRAR: filas bien formadas, plausibles, y
leidas como si fueran las pedidas.

Lo detecto el pane whatsappbot-final con dos controles, y el segundo es el que
importa: pedir un id INEXISTENTE devolvia la misma primera fila y el mismo orden
que pedir dos ids validos. Sin ese control negativo el sintoma se lee como "no lo
encontro y degrado", que es un problema distinto y mucho menos grave.

Fallaba hacia una respuesta creible en vez de hacia el error. De todas las
direcciones en las que un filtro puede fallar, esa es la peor: no hay sintoma.

POR ESO CADA TEST DE ABAJO LLEVA SU CONTROL NEGATIVO. Un test que solo verifica
que los ids pedidos aparecen pasaria igual con el filtro roto, porque con el
filtro roto aparece TODO — incluidos los pedidos.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService

ID_INEXISTENTE = 999_999_999


@pytest.fixture()
def svc(tmp_path: Path) -> MemoryService:
    s = MemoryService(tmp_path / "ids.db", workspace_root=tmp_path)
    s.init_db()
    return s


def _ingest(svc: MemoryService, text: str):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="test", locator="l", excerpt="e")],
        scope="project:test", source_agent="pytest", confidence=0.8,
    )


@pytest.fixture()
def claims(svc: MemoryService):
    return [_ingest(svc, f"Claim numero {i} sobre backups y recall.") for i in range(6)]


# --- store ----------------------------------------------------------------

def test_store_returns_exactly_the_requested_ids(svc, claims):
    pedidos = [claims[1].id, claims[4].id]
    devueltos = [c.id for c in svc.store.list_claims(ids=pedidos, limit=50)]
    assert sorted(devueltos) == sorted(pedidos)


def test_store_returns_nothing_for_an_id_that_does_not_exist(svc, claims):
    """EL CONTROL QUE DETECTA EL BUG. Con el filtro roto esto devuelve filas."""
    devueltos = svc.store.list_claims(ids=[ID_INEXISTENTE], limit=50)
    assert devueltos == [], (
        f"pedir un id inexistente devolvio {len(devueltos)} filas: el filtro no se "
        "esta aplicando y el llamador recibe claims ajenas como si fueran las suyas"
    )


def test_an_empty_id_list_means_none_not_everything(svc, claims):
    """`ids=[]` es "ninguna"; `ids=None` es "sin filtrar". Confundirlos devuelve el corpus."""
    assert svc.store.list_claims(ids=[], limit=50) == []
    assert len(svc.store.list_claims(ids=None, limit=50)) == len(claims)


def test_a_partially_valid_request_returns_only_what_exists(svc, claims):
    pedidos = [claims[0].id, ID_INEXISTENTE]
    devueltos = [c.id for c in svc.store.list_claims(ids=pedidos, limit=50)]
    assert devueltos == [claims[0].id]


# --- service, paginado y no paginado --------------------------------------

def test_service_list_claims_filters_by_id(svc, claims):
    pedidos = [claims[2].id]
    assert [c.id for c in svc.list_claims(ids=pedidos, limit=50)] == pedidos
    assert svc.list_claims(ids=[ID_INEXISTENTE], limit=50) == []


def test_service_paged_listing_filters_by_id(svc, claims):
    pedidos = [claims[3].id, claims[5].id]
    filas, _ = svc.list_claims_page(ids=pedidos, limit=50)
    assert sorted(c.id for c in filas) == sorted(pedidos)

    vacio, _ = svc.list_claims_page(ids=[ID_INEXISTENTE], limit=50)
    assert vacio == [], "el camino paginado ignora el filtro aunque el directo lo respete"


def test_the_filter_actually_narrows(svc, claims):
    """Contra-caso del control negativo: pedir un subconjunto devuelve MENOS que todo.

    Sin esto, un filtro que devolviera siempre vacio pasaria los tests de arriba.
    """
    todas = svc.store.list_claims(limit=50)
    subconjunto = svc.store.list_claims(ids=[claims[0].id], limit=50)
    assert 0 < len(subconjunto) < len(todas)
