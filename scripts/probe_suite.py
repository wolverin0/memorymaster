#!/usr/bin/env python3
"""Marcador de comportamiento — mide el sistema vivo, no el algoritmo en un cuarto limpio.

POR QUE EXISTE. El 2026-08-18 cinco bugs reales sobrevivieron a 4617 tests, diez
scripts de evaluacion y un audit automatizado completo. Uno tenia el 45% del indice
de busqueda podrido. Ninguna prueba podia verlos porque todas corren sobre corpus
sintetico y por un camino de recuperacion que ninguna superficie usa por defecto.
Este archivo es lo que falta: sondas contra la base REAL, cuya verdad no la define
el sistema medido.

INVARIANTES, y ninguna es decorativa:

  1. SOLO LECTURA. Nunca escribe en la base. No llama init_db (que migraria).
     Toda consulta pasa con record_accesses=False.

  2. TAMANO MINIMO DE MUESTRA POR SONDA, verificado en codigo. Medir menos casos
     sube el porcentaje sin que el sistema mejore, y es la forma mas barata de
     hacer trampa con una meta numerica. Por eso el minimo NO es configurable por
     linea de comandos: bajarlo requiere editar este archivo, que es kind
     evaluator-edit y nace bloqueado en el ledger.

  3. --check FALLA si una metrica empeora O si cualquier muestra cae bajo su
     minimo. Un marcador que no puede fallar es un reporte.

USO
    python scripts/probe_suite.py                 # mide y emite JSON
    python scripts/probe_suite.py --check         # compara contra la linea base
    python scripts/probe_suite.py --freeze        # escribe la linea base (una vez)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import statistics
import sys
import time

# Medir el codigo de ESTE repo, no el paquete instalado. Ejecutar
# `python scripts/probe_suite.py` pone scripts/ en sys.path, no el cwd, asi que
# `import memorymaster` resolvia a site-packages: el marcador media la version
# publicada y no podia ver ninguna mejora de la rama. Un loop de calidad que mide
# el paquete instalado nunca registra su propio progreso.
# (Tercera aparicion de esta misma trampa el 2026-08-19: tambien fingio una
# version vieja por un egg-info y casi hizo reportar un merge perdido.)
_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if "--installed" not in sys.argv and _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

BASELINE = pathlib.Path(__file__).parent / "probes" / "baseline.json"
GOALS = pathlib.Path(__file__).parent / "probes" / "goals.json"
COHORT = pathlib.Path(__file__).parent / "probes" / "cohort.json"
DEFAULT_DB = "memorymaster.db"
DEFAULT_SCOPE = "project:memorymaster"

# Minimos por sonda. Ver invariante 2: no se exponen como flags a proposito.
MIN_SAMPLE = {
    "self_retrieval": 250,
    "top1_relevance": 250,
    "index_false_positives": 200,
}

# COHORTE CONGELADA. Sin esto la sonda muestrea, y muestrear la volvia inutil:
# medido el 2026-08-19, cambiar solo la semilla movia found_first_pct entre 71,7 y
# 86,7 — un rango de 15 puntos contra una meta que pide subir 5,3. Con la poblacion
# ademas creciendo (la ventana ORDER BY id DESC se corre con cada claim nueva), la
# misma semilla daba 81,7 y minutos despues 71,7 sin que el sistema cambiara.
# Midiendo SIEMPRE las mismas claims no hay loteria de semilla ni deriva de
# poblacion, y la unica varianza que queda es la del sistema, que es la que importa.
COHORT_SIZE = 300

# El ruido que consume check_goals DEBE venir de corridas espaciadas. Tres corridas
# seguidas terminan en 30s sin que nada escriba en la base y reportan 0,0, y con un
# piso de 0,0 cualquier umbral se declara evaluable — la regla no protegeria nada.
# Espaciadas, con dreaming y steward ingestando normal, el spread real fue 0,6-0,7.
NOISE_RUN_GAP_SECONDS = 60
# Piso POR METRICA, no global: median_lex va de 0 a 1 y las demas en puntos
# porcentuales. Aplicar 0,7 a una escala 0-1 declaraba no evaluable una meta
# legitima — error cometido y corregido el 2026-08-19.
NOISE_FLOOR = {
    "self_retrieval.found_first_pct": 0.7,
    "self_retrieval.missed_pct": 0.7,
    "index_false_positives.pct": 0.7,
    "top1_relevance.median_lex": 0.01,
}

# Cuanto puede empeorar una metrica antes de que --check falle. Generoso para el
# ruido de muestreo, no para una regresion real.
TOLERANCE = {
    "self_retrieval.missed_pct": 1.5,        # puntos porcentuales
    "index_false_positives.pct": 1.0,        # puntos porcentuales
    "top1_relevance.median_lex": -0.02,      # caida maxima admitida
}

# Tokens que no pueden existir en ningun corpus. Si alguno aparece, cambiarlo.
NONSENSE = ["xyzzy", "plugh", "frobnicate"]


def _service(db: str):
    from memorymaster.core.service import MemoryService

    # Sin init_db a proposito: construir no migra, init_db si.
    return MemoryService(db, workspace_root=".")


def _ro(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------- cohorte

def cohort_rows(conn, scope: str) -> tuple[list, int]:
    """Las MISMAS claims en cada corrida. Devuelve (filas, cuantas se perdieron).

    Se eligen las mas VIEJAS que cumplen el filtro, no las mas nuevas: la cola
    reciente cambia con cada ingesta, y esa deriva fue justamente lo que hizo que
    la misma semilla diera 81,7 y minutos despues 71,7.

    Una claim de la cohorte que ya no aparece (archivada, borrada, cambiada de
    scope) NO se reemplaza en silencio — se cuenta y se reporta. Reponerla con otra
    claim seria elegir la muestra despues de ver el resultado.
    """
    if COHORT.exists():
        ids = json.loads(COHORT.read_text(encoding="utf-8"))["claim_ids"]
    else:
        ids = [r["id"] for r in conn.execute(
            """SELECT id FROM claims
               WHERE status='confirmed' AND scope=? AND length(text) BETWEEN 60 AND 600
               ORDER BY id ASC LIMIT ?""",
            (scope, COHORT_SIZE),
        ).fetchall()]
        COHORT.parent.mkdir(parents=True, exist_ok=True)
        COHORT.write_text(json.dumps(
            {"_head": "Cohorte congelada del marcador. Las mismas claims en cada corrida: "
                      "sin esto la sonda muestrea y el ruido supera a la meta. Cambiarla es "
                      "kind evaluator-edit.", "scope": scope, "size": len(ids),
             "claim_ids": ids}, indent=2) + "\n", encoding="utf-8")

    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, text FROM claims WHERE id IN ({placeholders})", ids
    ).fetchall() if ids else []
    return rows, len(ids) - len(rows)


# --------------------------------------------------------------------- sondas

def probe_self_retrieval(svc, conn, scope: str) -> dict:
    """Se encuentra una claim buscandola con sus propias palabras distintivas?

    La verdad es independiente del sistema: la sonda eligio las palabras, asi que
    el sistema no puede definir su propio exito.
    """
    from memorymaster.recall.recall_tokenizer import _candidate_tokens

    rows, lost = cohort_rows(conn, scope)

    df: dict[str, int] = {}
    for r in rows:
        for tok in set(_candidate_tokens(r["text"])):
            df[tok] = df.get(tok, 0) + 1

    at1 = at5 = missed = skipped = 0
    for r in rows:
        toks = sorted(set(_candidate_tokens(r["text"])), key=lambda t: (df.get(t, 0), -len(t)))
        if len(toks) < 2:
            skipped += 1
            continue
        hits = svc.query_rows(
            " ".join(toks[:2]), limit=5, scope_allowlist=[scope],
            include_candidates=True, record_accesses=False,
        )
        ids = [h["claim"].id for h in hits]
        if ids and ids[0] == r["id"]:
            at1 += 1
        elif r["id"] in ids:
            at5 += 1
        else:
            missed += 1

    total = at1 + at5 + missed
    return {
        "sample": total, "skipped_no_salient_terms": skipped,
        "cohort_claims_gone": lost,
        "found_first_pct": round(100 * at1 / total, 1) if total else None,
        "found_top5_pct": round(100 * (at1 + at5) / total, 1) if total else None,
        "missed_pct": round(100 * missed / total, 1) if total else None,
    }


def probe_nonsense_query(svc, scope: str) -> dict:
    """Una consulta de tokens inexistentes debe devolver cero, en TODOS los modos.

    Binario y sin tolerancia: no hay grado admisible de inventar resultados.
    """
    out = {}
    for mode in ("legacy", "hybrid"):
        try:
            rows = svc.query_rows(
                " ".join(NONSENSE), limit=5, scope_allowlist=[scope],
                include_candidates=True, retrieval_mode=mode, record_accesses=False,
            )
            out[mode] = len(rows)
        except Exception as exc:  # un modo roto no puede pasar como "cero resultados"
            out[mode] = f"error: {exc}"
    return {"results_per_mode": out, "sample": 1}


def probe_index_false_positives(conn, terms: list[str], per_term: int) -> dict:
    """El indice dice que estas claims contienen el termino. Contienen el termino?

    Solo cuenta los campos que el ranker puntua. Un match unicamente en predicate
    es un acierto legitimo del indice con relevancia cero, y se reporta aparte en
    vez de contarse como podredumbre.
    """
    checked = wrong = predicate_only = 0
    per: dict[str, float] = {}
    for term in terms:
        rows = conn.execute(
            """SELECT c.text, c.normalized_text, c.subject, c.predicate, c.object_value
               FROM claims c JOIN claims_fts f ON f.rowid = c.id
               WHERE f.claims_fts MATCH ? LIMIT ?""",
            (f'"{term}"', per_term),
        ).fetchall()
        bad = 0
        for r in rows:
            ranked = " ".join(str(r[k] or "") for k in
                              ("text", "normalized_text", "subject", "object_value")).lower()
            if term in ranked:
                continue
            if term in (r["predicate"] or "").lower():
                predicate_only += 1
            else:
                bad += 1
        checked += len(rows)
        wrong += bad
        per[term] = round(100 * bad / len(rows), 1) if rows else None
    return {
        "sample": checked, "pct": round(100 * wrong / checked, 1) if checked else None,
        "predicate_only_matches": predicate_only, "per_term_pct": per,
    }


def probe_top1_relevance(svc, conn, scope: str) -> dict:
    """Mediana de la relevancia lexica del PRIMER resultado sobre consultas reales.

    Si esta cerca de cero, el recall esta roto sin importar que diga ningun test.
    Corre sobre la MISMA cohorte congelada, por la misma razon.
    """
    from memorymaster.recall.recall_tokenizer import _candidate_tokens

    rows, _ = cohort_rows(conn, scope)

    lex: list[float] = []
    for r in rows:
        toks = _candidate_tokens(r["text"])
        if len(toks) < 3:
            continue
        hits = svc.query_rows(
            " ".join(toks[:3]), limit=1, scope_allowlist=[scope],
            include_candidates=True, record_accesses=False,
        )
        if hits:
            lex.append(float(hits[0].get("lexical_score") or 0.0))
    return {
        "sample": len(lex),
        "median_lex": round(statistics.median(lex), 3) if lex else None,
        "zero_lex_pct": round(100 * sum(1 for x in lex if x == 0) / len(lex), 1) if lex else None,
    }


def _dig(d: dict, path: str):
    """Lee 'probe.campo' o 'probe.campo.sub' del marcador."""
    cur = d.get("probes", {})
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def check_goals(now: dict) -> tuple[bool, list[str]]:
    """Criterio de salida del loop: todas las metas cumplidas Y sus contra-metricas.

    Una meta sin contra-metrica se alcanza por el camino barato. Aca la
    contra-metrica se evalua ANTES que la meta: si cayo, la meta no cuenta aunque
    su numero se vea bien.
    """
    if not GOALS.exists():
        return False, [f"no existe {GOALS}"]
    spec = json.loads(GOALS.read_text(encoding="utf-8"))
    lines, ok = [], True

    # Ruido medido al congelar: el spread entre corridas consecutivas sin que el
    # sistema cambiara. Un umbral por debajo de esto no es una meta, es una
    # loteria — el 2026-08-19 la sonda tenia 15 puntos de ruido contra una meta
    # que pedia subir 5,3, y se habria "cumplido" sin tocar una linea de codigo.
    noise = {}
    if BASELINE.exists():
        noise = json.loads(BASELINE.read_text(encoding="utf-8")).get("measured_noise", {})

    for g in sorted(spec["goals"], key=lambda x: x["priority"]):
        val = _dig(now, g["metric"])
        if val is None:
            lines.append(f"  ? {g['id']:<18} {g['metric']} no se pudo medir")
            ok = False
            continue

        # El margen que pide la meta tiene que superar al ruido medido, o la meta
        # no es evaluable: se cumple o falla por variacion, no por el sistema.
        margin = abs(float(g["target"]) - float(g["baseline"]))
        floor = noise.get(g["metric"])
        # Una meta de SOSTENER tiene margen cero por definicion (objetivo ==
        # linea base). No es una meta debil: es un piso, y quien la vigila es la
        # comparacion de regresion, no esta regla.
        #
        # La exencion exige la bandera EXPLICITA hold:true. Inferirla de que los
        # numeros empaten seria la puerta de atras: cualquier meta cuyo target
        # coincida por casualidad con su base quedaria exenta sin declararlo, y
        # asi es exactamente como se vacia una regla.
        is_hold = bool(g.get("hold")) and margin == 0.0
        if floor is not None and g["direction"] != "eq" and not is_hold and margin <= floor:
            lines.append(
                f"  ! {g['id']:<18} NO EVALUABLE: margen {margin:.3f} <= ruido medido "
                f"{floor:.3f}. Un umbral por debajo del ruido se cumple por azar.")
            ok = False
            continue

        if g.get("counter_metric"):
            cval = _dig(now, g["counter_metric"])
            crule, cnum = g["counter_rule"].split(":")
            cnum = float(cnum)
            if cval is None:
                broken = True
            elif crule == "gte":
                broken = cval < cnum
            else:
                broken = cval > cnum
            if broken:
                lines.append(f"  X {g['id']:<18} CONTRA-METRICA rota: "
                             f"{g['counter_metric']}={cval} viola {g['counter_rule']}")
                ok = False
                continue

        d = g["direction"]
        if d == "eq":
            met = val == g["target"]
        elif d == "gte":
            met = val >= g["target"]
        else:
            met = val <= g["target"]
        mark = "OK" if met else "  "
        arrow = "->" if not met else "=="
        lines.append(f"  {mark} {g['id']:<18} {g['metric']}={val} {arrow} objetivo {d} {g['target']}"
                     f"  (base {g['baseline']})")
        if not met:
            ok = False
    return ok, lines


# ---------------------------------------------------------------- comparacion

def evaluate(now: dict, base: dict | None) -> tuple[bool, list[str]]:
    """Devuelve (ok, motivos). Falla por regresion O por muestra insuficiente."""
    problems: list[str] = []

    for name, min_n in MIN_SAMPLE.items():
        got = (now["probes"].get(name) or {}).get("sample")
        if got is None or got < min_n:
            problems.append(
                f"{name}: muestra {got} < minimo {min_n} — una muestra recortada sube "
                f"el porcentaje sin que el sistema mejore")

    nonsense = now["probes"]["nonsense_query"]["results_per_mode"]
    for mode, count in nonsense.items():
        if count != 0:
            problems.append(f"nonsense_query[{mode}]={count} — debe ser 0 en todos los modos")

    if base:
        def cmp(path: str, higher_is_worse: bool):
            probe, field = path.split(".")
            a = (base["probes"].get(probe) or {}).get(field)
            b = (now["probes"].get(probe) or {}).get(field)
            if a is None or b is None:
                return
            tol = TOLERANCE[path]
            delta = b - a
            worse = delta > tol if higher_is_worse else delta < tol
            if worse:
                problems.append(f"{path}: {a} -> {b} (delta {delta:+.3f}, tolerancia {tol})")

        cmp("self_retrieval.missed_pct", True)
        cmp("index_false_positives.pct", True)
        cmp("top1_relevance.median_lex", False)

    return (not problems), problems


def measure(db: str, scope: str) -> dict:
    """Una corrida completa del marcador."""
    started = time.time()
    svc = _service(db)
    conn = _ro(db)
    out = {
        "generated_at_epoch": int(started), "db": db, "scope": scope,
        "probes": {
            "self_retrieval": probe_self_retrieval(svc, conn, scope),
            "nonsense_query": probe_nonsense_query(svc, scope),
            "index_false_positives": probe_index_false_positives(
                conn, ["deploy", "release", "backup", "sensitivity"], 300),
            "top1_relevance": probe_top1_relevance(svc, conn, scope),
        },
    }
    out["elapsed_seconds"] = round(time.time() - started, 1)
    conn.close()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--scope", default=DEFAULT_SCOPE)
    ap.add_argument("--check", action="store_true", help="compara contra la linea base; exit 1 si empeoro")
    ap.add_argument("--freeze", action="store_true", help="escribe la linea base")
    ap.add_argument("--reason", default="",
                    help="obligatorio al re-congelar: por que cambio el regimen medido")
    ap.add_argument("--installed", action="store_true",
                    help="medir el paquete instalado en vez del codigo de este repo")
    ap.add_argument("--goals", action="store_true",
                    help="criterio de salida del loop: exit 0 solo si TODAS las metas se cumplen")
    args = ap.parse_args(argv)

    scoreboard = measure(args.db, args.scope)

    if args.freeze:
        # RE-CONGELAR EXIGE MOTIVO. Blanquear es re-congelar para que un defecto
        # desaparezca de la medicion; re-basar legitimo es que el REGIMEN cambio
        # por una razon de sistema, el numero viejo midio otro regimen, y queda
        # registrado al lado del nuevo. La unica diferencia observable entre
        # ambos es si el valor anterior sobrevive con su motivo — asi que el
        # motivo se exige y el anterior se conserva, en vez de confiar en que
        # alguien se acuerde.
        prior = None
        if BASELINE.exists():
            if not args.reason.strip():
                print("re-congelar exige --reason: sin el motivo registrado, "
                      "re-basar es indistinguible de blanquear un defecto",
                      file=sys.stderr)
                return 1
            prior = json.loads(BASELINE.read_text(encoding="utf-8"))

        # Congelar NO es medir una vez. El ruido entre corridas es parte de la linea
        # base: sin el no se puede distinguir una meta de un azar. El 2026-08-19 la
        # sonda tenia 15 puntos de ruido contra una meta que pedia subir 5,3.
        NOISE_PATHS = ("self_retrieval.found_first_pct", "self_retrieval.missed_pct",
                       "top1_relevance.median_lex", "index_false_positives.pct")
        series = {k: [_dig(scoreboard, k)] for k in NOISE_PATHS}
        for _ in range(2):
            time.sleep(NOISE_RUN_GAP_SECONDS)  # ver NOISE_RUN_GAP_SECONDS
            again = measure(args.db, args.scope)
            for k in NOISE_PATHS:
                series[k].append(_dig(again, k))
        scoreboard["measured_noise"] = {
            k: max(round(max(v) - min(v), 3), NOISE_FLOOR.get(k, 0.0))
            for k, v in series.items()
            if all(x is not None for x in v)
        }
        if prior is not None:
            history = prior.pop("previous_baselines", [])
            history.append({
                "frozen_at_epoch": prior.get("generated_at_epoch"),
                "reason_superseded": args.reason.strip(),
                "metrics": {k: _dig(prior, k) for k in (
                    "self_retrieval.found_first_pct", "self_retrieval.found_top5_pct",
                    "self_retrieval.missed_pct", "top1_relevance.median_lex",
                    "index_false_positives.pct",
                    "nonsense_query.results_per_mode.hybrid")},
            })
            scoreboard["previous_baselines"] = history
        scoreboard["noise_gap_seconds"] = NOISE_RUN_GAP_SECONDS
        scoreboard["noise_floor_applied"] = NOISE_FLOOR
        scoreboard["noise_runs"] = 3
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(scoreboard, indent=2) + "\n", encoding="utf-8")
        print(f"linea base escrita en {BASELINE}")
        print("ruido medido en 3 corridas:", json.dumps(scoreboard["measured_noise"]))
        return 0

    if args.goals:
        met, lines = check_goals(scoreboard)
        print(json.dumps(scoreboard, indent=2, ensure_ascii=False))
        print(chr(10) + "METAS DE LA RONDA:", file=sys.stderr)
        for line in lines:
            print(line, file=sys.stderr)
        print(chr(10) + ('TODAS CUMPLIDAS' if met else 'FALTAN METAS'), file=sys.stderr)
        return 0 if met else 1

    base = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None
    ok, problems = evaluate(scoreboard, base if args.check else None)
    print(json.dumps(scoreboard, indent=2, ensure_ascii=False))

    if args.check:
        if not base:
            print("\nsin linea base: correr --freeze primero", file=sys.stderr)
            return 1
        if problems:
            print("\nMARCADOR EMPEORO:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print("\nmarcador ok: ninguna metrica empeoro", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
