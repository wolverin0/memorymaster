#!/usr/bin/env python3
"""Por que G1 no sube: es techo de la cohorte o defecto del recuperador?

LA PREGUNTA. La meta G1 mide `found_first_pct`: la sonda toma los DOS TOKENS MAS
RAROS de cada claim y pregunta si esa misma claim sale primera. Esta clavada en
60,7 mientras LongMemEval subio hit@1 de 0,342 a 0,506 sobre el mismo periodo.
Esa divergencia SUGIERE que el techo es una propiedad de la cohorte y no del
recuperador — pero sugerir no es medir, y cerrar la meta por deduccion seria
exactamente el error que el marcador existe para impedir.

LA CLASIFICACION. Para cada claim que NO sale primera, se mira quien gano:

  ambiguedad_legitima  el ganador contiene los DOS tokens de la consulta. La
                       consulta no distingue entre las dos claims, asi que
                       ninguna eleccion es incorrecta. Esto es TECHO: sube solo
                       cambiando la cohorte o la sonda, no el recuperador.
  descartada           la claim objetivo fue filtrada. drop_trace dice por que.
                       Esto es DEFECTO y se puede arreglar.
  orden                aparecio en el top-5 pero no primera, y el ganador NO
                       tiene los dos tokens. DEFECTO de ranking.
  ausente              no aparecio y nadie la descarto: no llego ni a candidata.
                       DEFECTO de recuperacion (indice o fanout).

NO TOCA scripts/probes/. Lee la cohorte congelada y reusa la misma construccion
de consulta importando desde probe_suite; no modifica ninguna sonda ni meta —
eso es evaluator-edit y es gate del operador.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter

# La raiz del repo, no scripts/. Ejecutar `python scripts/x.py` pone el directorio
# DEL SCRIPT en sys.path, con lo cual `import memorymaster` resolveria al paquete
# instalado y el diagnostico mediria otro arbol. Tercera vez que pasa en este repo.
REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _classify(target_id: int, target_text: str, hits: list, toks: list[str],
              drops: list[dict]) -> tuple[str, dict]:
    ids = [h["claim"].id for h in hits]
    if ids and ids[0] == target_id:
        return "primera", {}

    dropped = [d for d in drops if d.get("claim_id") == target_id]
    if dropped:
        return "descartada", {"razon": dropped[0].get("reason")}

    if not hits:
        return "ausente", {"nadie_gano": True}

    winner = hits[0]["claim"]
    wtext = (winner.text or "").lower()
    winner_has_both = all(t in wtext for t in toks)

    if winner_has_both:
        return "ambiguedad_legitima", {
            "ganadora": winner.id,
            "extracto": (winner.text or "")[:110],
        }
    if target_id in ids:
        return "orden", {"posicion": ids.index(target_id) + 1, "ganadora": winner.id}
    return "ausente", {"ganadora": winner.id}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="memorymaster.db")
    ap.add_argument("--scope", default="project:memorymaster")
    ap.add_argument("--show", type=int, default=6, help="ejemplos por categoria")
    args = ap.parse_args(argv)

    from memorymaster.recall import drop_trace
    from memorymaster.recall.recall_tokenizer import _candidate_tokens

    sys.path.insert(0, str(REPO / "scripts"))
    from probe_suite import _ro, _service, cohort_rows  # type: ignore

    svc = _service(args.db)
    conn = _ro(args.db)
    rows, lost = cohort_rows(conn, args.scope)

    df: dict[str, int] = {}
    for r in rows:
        for tok in set(_candidate_tokens(r["text"])):
            df[tok] = df.get(tok, 0) + 1

    tally: Counter[str] = Counter()
    examples: dict[str, list] = {}
    razones: Counter[str] = Counter()

    for r in rows:
        toks = sorted(set(_candidate_tokens(r["text"])), key=lambda t: (df.get(t, 0), -len(t)))
        if len(toks) < 2:
            continue
        query = " ".join(toks[:2])
        with drop_trace.recording() as drops:
            hits = svc.query_rows(
                query, limit=5, scope_allowlist=[args.scope],
                include_candidates=True, record_accesses=False,
            )
        verdict, detail = _classify(r["id"], r["text"], hits, toks[:2], drops)
        tally[verdict] += 1
        if verdict == "descartada":
            razones[str(detail.get("razon"))] += 1
        if verdict != "primera" and len(examples.setdefault(verdict, [])) < args.show:
            examples[verdict].append({
                "claim": r["id"], "consulta": query,
                "texto": (r["text"] or "")[:110], **detail,
            })

    total = sum(tally.values())
    fallos = total - tally["primera"]
    techo = tally["ambiguedad_legitima"]
    defectos = fallos - techo

    print(json.dumps({
        "cohorte": total,
        "claims_perdidas_de_la_cohorte": lost,
        "primera_pct": round(100 * tally["primera"] / total, 1) if total else None,
        "fallos": fallos,
        "de_los_fallos": {
            "techo_ambiguedad_legitima": techo,
            "defecto_descartada": tally["descartada"],
            "defecto_orden": tally["orden"],
            "defecto_ausente": tally["ausente"],
        },
        "razones_de_descarte": dict(razones),
        "techo_alcanzable_pct": round(100 * (tally["primera"] + techo) / total, 1) if total else None,
        "defectos_arreglables": defectos,
        "ejemplos": examples,
    }, indent=2, ensure_ascii=False))

    print(
        f"\nLECTURA: de {fallos} fallos, {techo} son ambiguedad legitima de la "
        f"cohorte (dos claims que la consulta no distingue) y {defectos} son "
        f"defectos con arreglo posible.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
