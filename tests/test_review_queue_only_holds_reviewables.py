"""La cola de revision contiene SOLO lo que necesita un humano.

QUE VIO EL OPERADOR (2026-08-26, dashboard real): una cola titulada "Stale or
flagged claims that need human review" llena de claims confirmed cuya "razon"
era `status=confirmed` — una no-razon — con prioridad 0.341 y nada que hacer.
Su reaccion literal: "i dont understand... this is not very intuitive". Tenia
razon: el filtro estaba INVERTIDO. `build_review_queue` escaneaba los primeros
N claims de cualquier estado y solo permitia EXCLUIR stale/conflicted, asi que
la cola se llenaba con lo que no necesitaba revision mientras los 5.976
conflicted reales quedaban fuera del escaneo.

EL CONTRATO NUEVO: pertenece a la cola lo que un humano puede accionar —
status stale, status conflicted, o una propuesta pendiente del steward
(los botones Approve/Reject). Una claim confirmed sin propuesta NO pertenece.

`test_stale_and_conflicted_do_enter` es el control positivo: sin el, una cola
que siempre devuelve vacio pasaria el resto del archivo.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.review import build_review_queue


@pytest.fixture()
def servicio(tmp_path: Path) -> MemoryService:
    svc = MemoryService(str(tmp_path / "review.db"), workspace_root=str(tmp_path))
    svc.init_db()
    return svc


def _ingest(svc: MemoryService, texto: str, sujeto: str) -> int:
    claim = svc.ingest(
        text=texto,
        citations=[CitationInput(source="session://t", locator="turn", excerpt=texto[:40])],
        subject=sujeto,
        predicate="estado",
        confidence=0.9,
    )
    return claim.id


def _poner_status(svc: MemoryService, claim_id: int, status: str) -> None:
    from memorymaster.core.lifecycle import transition_claim

    transition_claim(svc.store, claim_id, status, reason="fixture de test")


# --- el contrato -----------------------------------------------------------

def test_confirmed_without_a_proposal_stays_out(servicio: MemoryService):
    """El caso exacto de la queja: confirmed sin nada que hacer, fuera."""
    cid = _ingest(servicio, "claim confirmada comun que no necesita a nadie", "comun")
    _poner_status(servicio, cid, "confirmed")

    cola = build_review_queue(servicio, limit=50)

    assert cid not in {item.claim_id for item in cola}, (
        "una confirmed sin propuesta volvio a la cola de revision — el filtro "
        "invertido regreso"
    )


def test_stale_and_conflicted_do_enter(servicio: MemoryService):
    """Control positivo: sin esto, una cola siempre-vacia pasaria todo."""
    viejo = _ingest(servicio, "claim que decayo y espera revision humana", "decaida")
    _poner_status(servicio, viejo, "confirmed")
    _poner_status(servicio, viejo, "stale")
    choque = _ingest(servicio, "claim en conflicto que espera arbitraje", "choque")
    _poner_status(servicio, choque, "confirmed")
    _poner_status(servicio, choque, "conflicted")

    cola = build_review_queue(servicio, limit=50)
    por_id = {item.claim_id: item for item in cola}

    assert viejo in por_id and choque in por_id
    assert por_id[viejo].reason == "status=stale"
    assert por_id[choque].reason == "status=conflicted"


def test_a_flagged_confirmed_claim_enters_with_a_real_reason(servicio: MemoryService):
    """Confirmed + propuesta pendiente = adentro, y la razon LO DICE.

    Es donde viven los botones Approve/Reject del dashboard: si el flagged no
    entra, la propuesta queda invisible; si entra sin razon, vuelve el
    `status=confirmed` que confundio al operador.
    """
    cid = _ingest(servicio, "claim confirmada con una propuesta del steward encima", "flag")
    _poner_status(servicio, cid, "confirmed")

    cola = build_review_queue(servicio, limit=50, flagged_claim_ids={cid})
    por_id = {item.claim_id: item for item in cola}

    assert cid in por_id
    assert por_id[cid].reason == "pending steward proposal"
    assert "status=confirmed" not in por_id[cid].reason


def test_the_reason_never_says_status_confirmed(servicio: MemoryService):
    """La no-razon que disparo la queja, prohibida como propiedad general."""
    for sujeto in ("a", "b", "c"):
        cid = _ingest(servicio, f"claim confirmada {sujeto} sin motivo de revision", sujeto)
        _poner_status(servicio, cid, "confirmed")
    flag = _ingest(servicio, "confirmada con propuesta", "flag2")
    _poner_status(servicio, flag, "confirmed")

    cola = build_review_queue(servicio, limit=50, flagged_claim_ids={flag})

    for item in cola:
        assert item.reason != "status=confirmed", (
            f"claim {item.claim_id} entro a la cola con la no-razon"
        )


def test_exclusion_flags_still_work(servicio: MemoryService):
    viejo = _ingest(servicio, "stale que el operador pidio excluir", "excluida")
    _poner_status(servicio, viejo, "confirmed")
    _poner_status(servicio, viejo, "stale")

    cola = build_review_queue(servicio, limit=50, include_stale=False)

    assert viejo not in {item.claim_id for item in cola}
