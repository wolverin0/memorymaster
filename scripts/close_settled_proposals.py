#!/usr/bin/env python3
"""Cierra las propuestas del steward cuyo resultado YA ocurrio.

EL PROBLEMA QUE RESUELVE. La cola del steward mostraba 254 propuestas pendientes,
la mas vieja de cuatro meses, y se leia como una deuda de gobierno esperando a un
humano. Medida el 2026-08-20, la realidad era otra:

    119  la claim ya esta `archived` o `superseded` — la propuesta se cumplio
     90  propuesta de tipo `stale` sobre una claim que YA esta `stale`
     45  decisiones reales (superseded_candidate y conflicted sobre claims vivas)

O sea 209 de 254 son ARTEFACTO CONTABLE: el trabajo se hizo y nadie escribio el
evento que cierra la propuesta. El contador suma propuestas y nunca resta las que
se cumplen solas, asi que crece para siempre.

Es la señal inerte mas engañosa de todas las que apareceron estos dias. Las otras
callan; esta GRITA un numero grande y falso, y un numero falso hacia arriba es
peor que ninguno: manda a buscar trabajo donde no hay, y de paso entierra las 45
decisiones que si importan debajo de 209 que no.

POR QUE `apply_on_approve=False`. Aprobar con aplicacion re-ejecutaria la accion
sobre una claim que ya esta en el estado propuesto. Con False, steward.py saltea
`_apply_steward_approval` por completo (la condicion de la linea 1416) y lo unico
que se escribe es el evento de resolucion. NINGUN estado de claim cambia — este
script es contabilidad, no gobierno.

LO QUE NO TOCA. Las 45 decisiones reales quedan intactas a proposito: cerrarlas
exige juicio sobre si una supersesion o un conflicto se resuelve de un lado o del
otro, y eso no es contabilidad. Se listan al final para que se decidan a mano.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Estados de claim en los que la propuesta ya se cumplio y solo falta anotarlo.
SETTLED_STATUSES = {"archived", "superseded"}


def classify(svc, proposal: dict) -> tuple[str, str]:
    """(veredicto, motivo). `settled` = cerrable sin juicio; `real` = necesita decision.

    EL CRITERIO ES UNO SOLO: la claim ya esta en el estado que la propuesta pedia.
    No importa de que tipo sea la propuesta — si pedia `stale` y la claim esta
    `stale`, o pedia `superseded` y esta `superseded`, la recomendacion se cumplio
    y cerrarla no decide nada, solo lo anota.

    La primera version de esto buscaba la palabra "stale" dentro de un campo de
    texto y se perdia 90 propuestas porque ese campo no existe con ese nombre.
    Comparar contra `proposed_status` es lo que la propuesta realmente declara.
    """
    claim_id = proposal.get("claim_id")
    row = svc.store.get_claim(claim_id) if claim_id else None
    if row is None:
        return "settled", "la claim ya no existe"

    status = (getattr(row, "status", "") or "").lower()
    proposed = str(proposal.get("proposed_status") or "").strip().lower()

    if proposed and status == proposed:
        return "settled", f"la claim ya esta {status}, que es lo que la propuesta pedia"
    if status in SETTLED_STATUSES:
        return "settled", f"la claim ya esta {status} (terminal)"
    decision = str(proposal.get("proposal_decision") or "?")
    return "real", f"claim viva en {status}, la propuesta pide {proposed or '?'} | {decision}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="memorymaster.db")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--apply", action="store_true",
                    help="sin esta bandera solo informa; NADA se escribe")
    ap.add_argument("--limit", type=int, default=0, help="cerrar como mucho N (0 = todas)")
    args = ap.parse_args(argv)

    from memorymaster.core.service import MemoryService
    from memorymaster.govern.steward import (
        list_steward_proposals,
        resolve_steward_proposal,
    )

    svc = MemoryService(args.db, workspace_root=args.workspace)
    proposals = list_steward_proposals(svc, limit=2000, include_resolved=False)

    settled, real = [], []
    for p in proposals:
        verdict, why = classify(svc, p)
        (settled if verdict == "settled" else real).append((p, why))

    print(f"propuestas abiertas: {len(proposals)}")
    print(f"  cerrables sin juicio (contables): {len(settled)}")
    print(f"  decisiones reales:                {len(real)}")

    motivos = collections.Counter(why for _, why in settled)
    print("\nmotivos de cierre:")
    for k, n in motivos.most_common():
        print(f"  {n:>4}  {k}")

    if not args.apply:
        print("\n--- SIMULACION: no se escribio nada. Usar --apply para cerrarlas. ---")
    else:
        target = settled[: args.limit] if args.limit else settled
        ok = failed = 0
        for p, _ in target:
            try:
                resolve_steward_proposal(
                    svc, action="approve",
                    proposal_event_id=p.get("proposal_event_id"),
                    apply_on_approve=False,  # contabilidad: no re-aplica nada
                )
                ok += 1
            except Exception as exc:
                failed += 1
                print(f"  FALLO propuesta {p.get('proposal_event_id')}: {exc}", file=sys.stderr)
        print(f"\ncerradas: {ok}   fallidas: {failed}")

    print("\n=== DECISIONES REALES, que NO se tocan ===")
    por_tipo = collections.Counter(why.split("|")[-1].strip() for _, why in real)
    for k, n in por_tipo.most_common():
        print(f"  {n:>4}  {k}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
