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

def test_an_orphan_conflict_waits_for_revalidation(svc):
    huerfano = _claim(svc, "conflictiva cuyo rival ya no esta confirmado en ningun lado")
    _status(svc, huerfano, "conflicted")

    r = curation_drain.drain_conflicts(svc, apply=True)

    assert r["pending_revalidation"] == 1
    assert r["resolved_orphan"] == 0
    assert _get(svc, huerfano).status == "conflicted", (
        "la ausencia de rival no demuestra falta de vigencia"
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
    assert _get(svc, de_user).status == "conflicted"
    assert r["pending_revalidation"] == 1


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
    assert r["resolved_lost"] == 1 and r["pending_revalidation"] == 1
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
        lambda service, limit, include_resolved, **_kwargs: [
            {"proposal_event_id": 1, "claim_id": de_user},
            {"proposal_event_id": 2, "claim_id": normal},
        ],
    )
    aprobadas: list[int] = []
    monkeypatch.setattr(
        "memorymaster.govern.steward.resolve_steward_proposal",
        lambda service, action, proposal_event_id, apply_on_approve, actor: aprobadas.append(proposal_event_id),
    )

    r = curation_drain.drain_proposals(svc, apply=True)

    assert r["kept_for_operator"] == 1
    assert aprobadas == [2], f"se aprobo una propuesta del operador: {aprobadas}"


# --- T-1354: autores que no se auto-promueven ------------------------------

def test_a_stop_hook_winner_is_not_promoted_and_does_not_supersede_the_rival(svc):
    rival = _claim(svc, "la confirmada vieja con menos confianza",
                   subject="s", predicate="p", object_value="v1", confidence=0.3)
    _status(svc, rival, "confirmed")
    ganador = svc.ingest(
        text="la conflictiva nueva del stop hook con mas confianza",
        citations=[CitationInput(source="session://t", locator="turn", excerpt="stop hook")],
        subject="s", predicate="p", object_value="v2", scope="project:x", confidence=0.95,
        source_agent="llm-stop-hook",
    ).id
    _status(svc, ganador, "conflicted")

    r = curation_drain.drain_conflicts(svc, apply=True)

    assert r["held_by_source_policy"] == 1 and r["resolved_won"] == 0
    assert _get(svc, ganador).status == "conflicted"
    assert _get(svc, rival).status == "confirmed", (
        "la rival no puede quedar superseded por una promocion que no va a ocurrir"
    )


def test_a_predicate_without_subject_winner_is_not_promoted_and_does_not_supersede_the_rival(svc):
    """T-1591: a shifted-field claim is held before the supersession, like a held author."""
    rival = _claim(svc, "la confirmada vieja con menos confianza",
                   subject="s", predicate="p", object_value="v1", confidence=0.3)
    _status(svc, rival, "confirmed")
    ganador = _claim(svc, "la conflictiva nueva con el predicado corrido de campo",
                     predicate="p", object_value="v2", confidence=0.95)
    _status(svc, ganador, "conflicted")
    # sin sujeto no hay rival por tupla; el dren la encuentra en la auditoria
    svc.store.record_event(claim_id=ganador, event_type="validator",
                           details=f"conflicts_with_confirmed_claim:{rival}")

    r = curation_drain.drain_conflicts(svc, apply=True)

    assert r["held_by_structure_policy"] == 1 and r["resolved_won"] == 0
    assert _get(svc, ganador).status == "conflicted"
    assert _get(svc, rival).status == "confirmed"


# --- T-1686: propuestas que ya no pueden aplicar ----------------------------

def test_proposal_drain_leaves_alone_a_stale_proposal_on_a_superseded_claim(svc):
    """13 claims supersedidas por el rescope del 2026-10-06 cargaban una
    propuesta stale anterior: el dren la re-aprobaba en cada ciclo, la
    transicion superseded -> stale fallaba y quedaba un
    `steward_proposal_apply_failed` por claim y ciclo (52 por dia)."""
    old = _claim(svc, "la claim vieja que decayo")
    _status(svc, old, "confirmed")
    svc.store.record_event(
        claim_id=old, event_type="policy_decision", details="steward_proposal:stale",
        payload={"source": "steward", "decision": "stale", "proposed_status": "stale", "priority": 0.9},
    )
    newer = _claim(svc, "la claim nueva que la reemplaza")
    _status(svc, newer, "confirmed")
    svc.store.mark_superseded(old_claim_id=old, new_claim_id=newer, reason="rescope")

    r = curation_drain.drain_proposals(svc, apply=True)

    assert r["approved"] == 0 and r["failed"] == 0, r
    fallidas = [e for e in svc.list_events(event_type="audit", limit=50)
                if e.details == "steward_proposal_apply_failed"]
    assert fallidas == [], "el dren volvio a intentar una transicion imposible"
    assert _get(svc, old).status == "superseded"


def test_proposal_drain_counts_an_unapplied_approval_as_failed(svc, monkeypatch):
    """`resolve_steward_proposal` devuelve resolved=False cuando la aprobacion
    no aplico; el dren la sumaba como approved igual (13 aprobaciones
    fantasma por ciclo en el resumen)."""
    normal = _claim(svc, "claim operativa con propuesta encima")
    monkeypatch.setattr(
        "memorymaster.govern.steward.list_steward_proposals",
        lambda service, limit, include_resolved, **_kwargs: [{"proposal_event_id": 7, "claim_id": normal}],
    )
    monkeypatch.setattr(
        "memorymaster.govern.steward.resolve_steward_proposal",
        lambda service, action, proposal_event_id, apply_on_approve, actor: {
            "ok": True, "resolved": False, "applied": False,
        },
    )

    r = curation_drain.drain_proposals(svc, apply=True)

    assert r["approved"] == 0 and r["failed"] == 1, r
