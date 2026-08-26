"""Dren de curacion: la maquina resuelve lo que no es del operador.

POR QUE EXISTE (autorizado por el operador el 2026-08-26). La cola de revision
acumulaba 5.976 claims en conflicto y 27 propuestas del steward, y la reaccion
del operador al verla fue literal: "no puedo hacer 200 de esas manualidades
sobre cosas que no tienen sentido humano". Tenia razon, medido: solo 72 de
5.976 (1,2%) tocan scope=user o claims pineadas — el resto es tecnica operativa
de agentes (diagnosticos de red, SSRF, Docker) que ninguna persona deberia
arbitrar a mano.

EL CONTRATO:
- La maquina resuelve SOLO lo no-humano: quedan excluidos scope=user y todo lo
  pineado, que siguen siendo del operador en el dashboard.
- Resolucion DETERMINISTICA, sin LLM: el rival se localiza por la misma tupla
  del indice unico de confirmadas (tenant, subject, predicate, scope) o por el
  evento `conflicts_with_confirmed_claim:<id>`; gana `_pick_winner` (pinned >
  confianza > frescura > citas > id).
- TRES VEREDICTOS deterministas, medidos en dry-run sobre la base viva antes
  de elegirlos (5.976 conflicted: 260 pierden, 1.422 ganarian, 4.222 sin rival):
    (a) el conflicted PIERDE  -> superseded con puntero al ganador;
    (b) el conflicted GANA    -> la rival confirmada queda superseded por el
        y el ganador se promueve conflicted->confirmed (el orden importa: la
        supersesion libera primero la tupla del indice unico de confirmadas);
    (c) SIN rival confirmado vigente -> el conflicto es huerfano (aquello con
        lo que chocaba ya no esta confirmado): pasa a stale, que es reversible
        (stale->confirmed si se revalida) y deja que el decay lo gestione.
- Nada se borra: superseded conserva el texto y el puntero; stale decae por
  el camino normal. Todo emite evento de auditoria.
- Las propuestas del steward se aprueban con el camino existente
  (`resolve_steward_proposal`), con las mismas exclusiones.

El default es DRY-RUN: aplicar exige `apply=True`, y el resumen dice cuanto
quedo afuera y por que — un dren que no reporta lo que NO dreno seria otra
senal verde que no ejerce su camino.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from memorymaster.govern.conflict_resolver import (
    SupersessionRaceLost,
    _pick_winner,
    supersede_claim,
)

logger = logging.getLogger(__name__)

_RIVAL_IN_DETAILS = re.compile(r"conflicts_with_confirmed_claim:(\d+)")

DRAIN_DETAILS = "curation_drain"


def _is_operator_owned(claim: Any) -> bool:
    """Propiedad del operador = PINNED, y nada mas.

    La version del mismo dia usaba tambien scope=user como proxy de "esto es
    del operador", y el operador la refuto leyendo su cola: de 72 conflicted
    con scope=user, 70 los habia escrito atlas-llm-extractor — extraccion
    automatica, con la misma claim duplicada hasta cuatro veces con distinto
    id. La etiqueta de scope la asigna el extractor, no el humano; tratarla
    como eleccion humana le sirvio 72 items de maquina como si fueran suyos
    ("THERES NOT A SINGLE ONE THAT I CAN USE FOR NOTHING"). El unico acto
    deliberado del operador en el modelo de datos es el pin.
    """
    return bool(claim.pinned)


def _find_rival(service: Any, claim: Any) -> Any | None:
    """Rival = la confirmada contra la que este conflicted choca."""
    store = service.store
    if claim.subject and claim.predicate:
        with store.connect() as conn:
            row = conn.execute(
                """SELECT id FROM claims
                   WHERE COALESCE(tenant_id,'') = COALESCE(?, '')
                     AND subject = ? AND predicate = ? AND scope = ?
                     AND status = 'confirmed' AND visibility = 'public'
                     AND id != ?
                   LIMIT 1""",
                (claim.tenant_id, claim.subject, claim.predicate, claim.scope, claim.id),
            ).fetchone()
        if row is not None:
            return store.get_claim(int(row["id"]), include_citations=True)
    for event in store.list_events(claim_id=claim.id, limit=20):
        match = _RIVAL_IN_DETAILS.search(str(event.details or ""))
        if match:
            rival = store.get_claim(int(match.group(1)), include_citations=True)
            if rival is not None and rival.status == "confirmed":
                return rival
    return None


def drain_conflicts(service: Any, *, limit: int = 500, apply: bool = False) -> dict[str, Any]:
    from memorymaster.core.lifecycle import transition_claim

    store = service.store
    conflicted = store.find_by_status("conflicted", limit=limit, include_citations=True)
    summary: dict[str, Any] = {
        "scanned": len(conflicted),
        "resolved_lost": 0,
        "resolved_won": 0,
        "resolved_orphan": 0,
        "kept_for_operator": 0,
        "skipped_race": 0,
        "dry_run": not apply,
    }
    for claim in conflicted:
        if _is_operator_owned(claim):
            summary["kept_for_operator"] += 1
            continue
        rival = _find_rival(service, claim)
        try:
            if rival is None:
                # (c) conflicto huerfano: aquello con lo que chocaba ya no esta
                # confirmado. stale es reversible y el decay lo gestiona.
                if apply:
                    transition_claim(
                        store, claim.id, "stale",
                        reason=f"{DRAIN_DETAILS}: conflicto sin rival confirmado vigente",
                    )
                summary["resolved_orphan"] += 1
                continue
            pair = _pick_winner(claim, rival)
            if pair.loser.id == claim.id:
                # (a) el conflicted pierde contra la confirmada.
                if apply:
                    supersede_claim(
                        store,
                        old_claim_id=claim.id,
                        new_claim_id=rival.id,
                        reason=f"{DRAIN_DETAILS}: pierde contra confirmada #{rival.id} ({pair.reason})",
                    )
                summary["resolved_lost"] += 1
            else:
                # (b) el conflicted gana: primero la supersesion (libera la
                # tupla del indice unico de confirmadas), despues la promocion.
                if apply:
                    supersede_claim(
                        store,
                        old_claim_id=rival.id,
                        new_claim_id=claim.id,
                        reason=f"{DRAIN_DETAILS}: superada por conflicted ganador #{claim.id} ({pair.reason})",
                    )
                    transition_claim(
                        store, claim.id, "confirmed",
                        reason=f"{DRAIN_DETAILS}: gana el conflicto contra #{rival.id} ({pair.reason})",
                    )
                summary["resolved_won"] += 1
        except SupersessionRaceLost:
            summary["skipped_race"] += 1
        except Exception as exc:  # noqa: BLE001 - una claim rota no frena el dren
            logger.warning("curation drain skipped claim %d: %s", claim.id, exc)
            summary["skipped_race"] += 1
    return summary


def drain_proposals(service: Any, *, limit: int = 100, apply: bool = False) -> dict[str, Any]:
    from memorymaster.govern.steward import list_steward_proposals, resolve_steward_proposal

    proposals = list_steward_proposals(service, limit=limit, include_resolved=False)
    summary: dict[str, Any] = {
        "scanned": len(proposals),
        "approved": 0,
        "kept_for_operator": 0,
        "failed": 0,
        "dry_run": not apply,
    }
    for proposal in proposals:
        claim_id = proposal.get("claim_id")
        claim = service.store.get_claim(int(claim_id), include_citations=False) if claim_id else None
        if claim is None or _is_operator_owned(claim):
            summary["kept_for_operator"] += 1
            continue
        if not apply:
            summary["approved"] += 1
            continue
        try:
            resolve_steward_proposal(
                service,
                action="approve",
                proposal_event_id=int(proposal["proposal_event_id"]),
                apply_on_approve=True,
            )
            summary["approved"] += 1
        except Exception as exc:  # noqa: BLE001 - una propuesta rota no frena el dren
            logger.warning("curation drain proposal %s failed: %s", proposal.get("proposal_event_id"), exc)
            summary["failed"] += 1
    return summary


def run(service: Any, *, limit: int = 500, apply: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "conflicts": drain_conflicts(service, limit=limit, apply=apply),
        "proposals": drain_proposals(service, limit=min(limit, 200), apply=apply),
    }
