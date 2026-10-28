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
  evento `conflicts_with_confirmed_claim:<id>`; gana el vinculo explicito de
  supersesion o el pin (ver T-1863 abajo; ya no gana la confianza).
- TRES VEREDICTOS deterministas, medidos en dry-run sobre la base viva antes
  de elegirlos (5.976 conflicted: 260 pierden, 1.422 ganarian, 4.222 sin rival):
    (a) el conflicted PIERDE  -> superseded con puntero al ganador;
    (b) el conflicted GANA    -> la rival confirmada queda superseded por el
        y el ganador se promueve conflicted->confirmed en una transaccion;
    (c) SIN rival confirmado vigente -> el conflicto queda pendiente de
        revalidacion por el validator. La ausencia de rival no demuestra que
        la claim sea vieja ni que haya perdido evidencia.
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

from memorymaster.core.models import tuple_key
from memorymaster.core.promotion_policy import auto_promotion_blocked, structure_blocks_auto_promotion
from memorymaster.govern.conflict_resolver import (
    SupersessionRaceLost,
    _verdict,
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
                     AND (tuple_key = ? OR (tuple_key IS NULL
                          AND (subject = ? OR lower(trim(subject)) = ?)
                          AND (predicate = ? OR lower(trim(predicate)) = ?)))
                     AND scope = ?
                     AND status = 'confirmed' AND visibility = 'public'
                     AND id != ?
                   LIMIT 1""",
                (
                    claim.tenant_id,
                    tuple_key(claim.subject, claim.predicate),
                    claim.subject, claim.subject.strip().lower(),
                    claim.predicate, claim.predicate.strip().lower(),
                    claim.scope, claim.id,
                ),
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
        "pending_revalidation": 0,
        "held_for_review": 0,
        "kept_for_operator": 0,
        "held_by_source_policy": 0,
        "held_by_structure_policy": 0,
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
                # No current conflict is not evidence of staleness. Preserve
                # status until the normal validator checks evidence/policy.
                summary["pending_revalidation"] += 1
                continue
            decided = _verdict(claim, rival)
            if decided is None:
                # T-1863 F5: different (or unknown) values and no explicit
                # supersession link. Confidence/recency/citation counts are not
                # evidence of which side is true; a newer correction at .69 must
                # not lose to the older claim it corrects at .79. Leave both
                # untouched for review.
                summary["held_for_review"] += 1
                continue
            verdict, verdict_reason = decided
            if verdict == "claim_loses":
                # (a) el conflicted pierde contra la confirmada.
                if apply:
                    supersede_claim(
                        store,
                        old_claim_id=claim.id,
                        new_claim_id=rival.id,
                        reason=f"{DRAIN_DETAILS}: pierde contra confirmada #{rival.id} ({verdict_reason})",
                    )
                summary["resolved_lost"] += 1
            else:
                # (b) Supersession and confirmation share the lifecycle's
                # transaction, including source review and rollback on failure.
                if auto_promotion_blocked(claim.source_agent):
                    # T-1354: este autor no se promueve a confirmed por un camino
                    # automatico. Se corta ANTES de la supersesion: superar a la
                    # rival y despues fallar la promocion dejaria la tupla vacia.
                    summary["held_by_source_policy"] += 1
                    continue
                if structure_blocks_auto_promotion(claim.subject, claim.predicate):
                    # T-1591: mismo corte previo a la supersesion para un predicado
                    # sin sujeto (campo corrido, no una tripleta).
                    summary["held_by_structure_policy"] += 1
                    continue
                if apply:
                    transition_claim(
                        store, claim.id, "confirmed",
                        reason=f"{DRAIN_DETAILS}: gana el conflicto contra #{rival.id} ({verdict_reason})",
                        supersedes_claim_id=rival.id,
                    )
                summary["resolved_won"] += 1
        except SupersessionRaceLost:
            summary["skipped_race"] += 1
        except Exception as exc:  # noqa: BLE001 - una claim rota no frena el dren
            logger.warning("curation drain skipped claim %d: %s", claim.id, exc)
            summary["skipped_race"] += 1
    return summary


def drain_proposals(service: Any, *, limit: int = 100, apply: bool = False) -> dict[str, Any]:
    from memorymaster.govern.steward import (
        is_jev_proposal,
        is_operator_only_proposal,
        list_steward_proposals,
        resolve_steward_proposal,
    )

    # Jev proposals stay for the operator; they must not use up ``limit``.
    proposals = list_steward_proposals(
        service, limit=limit, include_resolved=False,
        exclude_from_limit=lambda proposal: is_operator_only_proposal(proposal.get("payload")),
    )
    summary: dict[str, Any] = {
        "scanned": len(proposals),
        "approved": 0,
        "kept_for_operator": 0,
        "kept_jev": 0,
        "failed": 0,
        "dry_run": not apply,
    }
    for proposal in proposals:
        if is_operator_only_proposal(proposal.get("payload")):
            # F-21: las propuestas de Jev (y las de rapidfuzz, T-0773) esperan
            # veredicto humano; el dren nunca las aprueba (quedan en la cola).
            summary["kept_for_operator"] += 1
            if is_jev_proposal(proposal.get("payload")):
                summary["kept_jev"] += 1
            continue
        claim_id = proposal.get("claim_id")
        claim = service.store.get_claim(int(claim_id), include_citations=False) if claim_id else None
        if claim is None or _is_operator_owned(claim):
            summary["kept_for_operator"] += 1
            continue
        if not apply:
            summary["approved"] += 1
            continue
        try:
            result = resolve_steward_proposal(
                service,
                action="approve",
                proposal_event_id=int(proposal["proposal_event_id"]),
                apply_on_approve=True,
                actor="automation",
            )
            # Una aprobacion que no aplico (resolved=False) deja la propuesta
            # pendiente; contarla como approved inflaba el resumen (T-1686).
            if isinstance(result, dict) and result.get("resolved") is False:
                summary["failed"] += 1
            else:
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
