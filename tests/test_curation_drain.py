"""El dren de curacion resuelve lo de la maquina y no toca lo del operador.

AUTORIZACION Y MEDICION (2026-08-26). El operador, ante 5.976 conflictos y 27
propuestas en su cola: "no puedo hacer 200 de esas manualidades sobre cosas que
no tienen sentido humano". Medido: solo 72 de 5.976 (1,2%) tocan scope=user o
pinned. Autorizo explicitamente que la maquina resuelva el resto, con auditoria
y transiciones reversibles, relajando el "nunca aplica" del ruling MM6.

EL TEST QUE MAS IMPORTA ES `test_operator_claims_are_never_touched`: la unica
regla inviolable del dren es la exclusion. Un dren que un dia toque una claim
pineada o de scope user esta reescribiendo la memoria del operador sin permiso
— por eso ese caso cubre los DOS veredictos destructivos (perder y quedar
huerfano) sobre claims excluidas.

Y `test_dry_run_mutates_nothing` es el contrato del default: el mismo codigo
que cuenta 5.976 resoluciones no puede haber aplicado ninguna sin --apply.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import curation_drain


@pytest.fixture()
def svc(tmp_path: Path) -> MemoryService:
    s = MemoryService(str(tmp_path / "drain.db"), workspace_root=str(tmp_path))
    s.init_db()
    return s


def _claim(svc, texto, *, subject=None, predicate=None, object_value=None,
           scope="project:x", confidence=0.8):
    return svc.ingest(
        text=texto,
        citations=[CitationInput(source="session://t", locator="turn", excerpt=texto[:30])],
        subject=subject, predicate=predicate, object_value=object_value,
        scope=scope, confidence=confidence,
    ).id


def _status(svc, claim_id, *estados):
    from memorymaster.core.lifecycle import transition_claim

    for estado in estados:
        transition_claim(svc.store, claim_id, estado, reason="fixture")


def _get(svc, claim_id):
    return svc.store.get_claim(claim_id, include_citations=False)


# --- veredicto (a): el conflicted pierde -----------------------------------

def test_a_losing_conflicted_claim_is_superseded_by_its_rival(svc):
    rival = _claim(svc, "la version confirmada y mas confiable del hecho",
                   subject="s", predicate="p", object_value="v1", confidence=0.9)
    _status(svc, rival, "confirmed")
    perdedor = _claim(svc, "la version conflictiva mas debil del mismo hecho",
                      subject="s", predicate="p", object_value="v2", confidence=0.3)
    _status(svc, perdedor, "conflicted")

    r = curation_drain.drain_conflicts(svc, apply=True)

    assert r["resolved_lost"] == 1
    despues = _get(svc, perdedor)
    assert despues.status == "superseded"
    assert despues.replaced_by_claim_id == rival
    assert _get(svc, rival).status == "confirmed"


# --- veredicto (b): el conflicted gana -------------------------------------

def test_a_winning_conflicted_claim_is_promoted_and_the_rival_superseded(svc):
    rival = _claim(svc, "la confirmada vieja con menos confianza",
                   subject="s", predicate="p", object_value="v1", confidence=0.3)
    _status(svc, rival, "confirmed")
    ganador = _claim(svc, "la conflictiva nueva con mas confianza",
                     subject="s", predicate="p", object_value="v2", confidence=0.95)
    _status(svc, ganador, "conflicted")

    r = curation_drain.drain_conflicts(svc, apply=True)

    assert r["resolved_won"] == 1
    assert _get(svc, ganador).status == "confirmed", (
        "el ganador quedo en conflicted: la promocion no ocurrio y el conflicto "
        "sigue vivo con otro nombre"
    )
    viejo = _get(svc, rival)
    assert viejo.status == "superseded"
    assert viejo.replaced_by_claim_id == ganador


# --- veredicto (c): huerfano -----------------------------------------------

def test_an_orphan_conflict_goes_stale_not_deleted(svc):
    huerfano = _claim(svc, "conflictiva cuyo rival ya no esta confirmado en ningun lado")
    _status(svc, huerfano, "conflicted")

    r = curation_drain.drain_conflicts(svc, apply=True)

    assert r["resolved_orphan"] == 1
    assert _get(svc, huerfano).status == "stale", (
        "el huerfano debia ir a stale (reversible, lo gestiona el decay)"
    )


# --- la regla inviolable ----------------------------------------------------

def test_operator_claims_are_never_touched(svc):
    """Solo lo PINEADO es del operador. scope=user NO protege.

    La primera version de la exclusion trataba scope=user como propiedad del
    operador; el operador la refuto leyendo su cola: 70 de 72 eran del
    extractor automatico con duplicados literales. La etiqueta de scope la
    pone la maquina; el unico acto deliberado del humano es el pin.
    """
    pineada = _claim(svc, "claim pineada por el operador en conflicto huerfano")
    _status(svc, pineada, "conflicted")
    svc.store.pin(pineada, True) if hasattr(svc.store, "pin") else svc.pin(pineada, True)

    de_user = _claim(svc, "observacion user-scope escrita por el extractor", scope="user")
    _status(svc, de_user, "conflicted")

    r = curation_drain.drain_conflicts(svc, apply=True)

    assert r["kept_for_operator"] == 1
    assert _get(svc, pineada).status == "conflicted", "toco una claim PINEADA"
    assert _get(svc, de_user).status == "stale", (
        "scope=user sin rival debia drenarse como huerfano; volvio el proxy falso"
    )


def test_dry_run_mutates_nothing(svc):
    rival = _claim(svc, "confirmada fuerte", subject="s", predicate="p",
                   object_value="v1", confidence=0.9)
    _status(svc, rival, "confirmed")
    perdedor = _claim(svc, "conflictiva debil", subject="s", predicate="p",
                      object_value="v2", confidence=0.2)
    _status(svc, perdedor, "conflicted")
    huerfano = _claim(svc, "huerfana sin rival vigente")
    _status(svc, huerfano, "conflicted")

    r = curation_drain.drain_conflicts(svc, apply=False)

    assert r["dry_run"] is True
    assert r["resolved_lost"] == 1 and r["resolved_orphan"] == 1
    assert _get(svc, perdedor).status == "conflicted", "el dry-run aplico"
    assert _get(svc, huerfano).status == "conflicted", "el dry-run aplico"


# --- propuestas -------------------------------------------------------------

def test_proposal_drain_keeps_operator_claims(svc, monkeypatch):
    """Las propuestas sobre claims PINEADAS no se auto-aprueban."""
    de_user = _claim(svc, "claim pineada con propuesta encima")
    svc.pin(de_user, True)
    normal = _claim(svc, "claim operativa con propuesta encima")

    monkeypatch.setattr(
        "memorymaster.govern.steward.list_steward_proposals",
        lambda service, limit, include_resolved: [
            {"proposal_event_id": 1, "claim_id": de_user},
            {"proposal_event_id": 2, "claim_id": normal},
        ],
    )
    aprobadas: list[int] = []
    monkeypatch.setattr(
        "memorymaster.govern.steward.resolve_steward_proposal",
        lambda service, action, proposal_event_id, apply_on_approve: aprobadas.append(proposal_event_id),
    )

    r = curation_drain.drain_proposals(svc, apply=True)

    assert r["kept_for_operator"] == 1
    assert aprobadas == [2], f"se aprobo una propuesta del operador: {aprobadas}"
