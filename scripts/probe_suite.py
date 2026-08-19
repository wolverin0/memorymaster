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
import random
import sqlite3
import statistics
import sys
import time

BASELINE = pathlib.Path(__file__).parent / "probes" / "baseline.json"
DEFAULT_DB = "memorymaster.db"
DEFAULT_SCOPE = "project:memorymaster"

# Minimos por sonda. Ver invariante 2: no se exponen como flags a proposito.
MIN_SAMPLE = {
    "self_retrieval": 50,
    "top1_relevance": 50,
    "index_false_positives": 200,
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


# --------------------------------------------------------------------- sondas

def probe_self_retrieval(svc, conn, scope: str, n: int, seed: int) -> dict:
    """Se encuentra una claim buscandola con sus propias palabras distintivas?

    La verdad es independiente del sistema: la sonda eligio las palabras, asi que
    el sistema no puede definir su propio exito.
    """
    from memorymaster.recall.recall_tokenizer import _candidate_tokens

    rows = conn.execute(
        """SELECT id, text FROM claims
           WHERE status='confirmed' AND scope=? AND length(text) BETWEEN 60 AND 600
           ORDER BY id DESC LIMIT 4000""",
        (scope,),
    ).fetchall()

    df: dict[str, int] = {}
    for r in rows:
        for tok in set(_candidate_tokens(r["text"])):
            df[tok] = df.get(tok, 0) + 1

    random.Random(seed).shuffle(rows := list(rows))
    at1 = at5 = missed = skipped = 0
    for r in rows:
        if at1 + at5 + missed >= n:
            break
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


def probe_top1_relevance(svc, conn, scope: str, n: int, seed: int) -> dict:
    """Mediana de la relevancia lexica del PRIMER resultado sobre consultas reales.

    Si esta cerca de cero, el recall esta roto sin importar que diga ningun test.
    """
    from memorymaster.recall.recall_tokenizer import _candidate_tokens

    rows = conn.execute(
        """SELECT text FROM claims WHERE status='confirmed' AND scope=?
           AND length(text) BETWEEN 60 AND 600 ORDER BY id DESC LIMIT 2000""",
        (scope,),
    ).fetchall()
    random.Random(seed).shuffle(rows := list(rows))

    lex: list[float] = []
    for r in rows:
        if len(lex) >= n:
            break
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--scope", default=DEFAULT_SCOPE)
    ap.add_argument("--seed", type=int, default=20260819,
                    help="fija la muestra; variarlo detecta sobreajuste a una muestra")
    ap.add_argument("--check", action="store_true", help="compara contra la linea base; exit 1 si empeoro")
    ap.add_argument("--freeze", action="store_true", help="escribe la linea base")
    args = ap.parse_args(argv)

    started = time.time()
    svc = _service(args.db)
    conn = _ro(args.db)

    scoreboard = {
        "generated_at_epoch": int(started),
        "db": args.db, "scope": args.scope, "seed": args.seed,
        "probes": {
            "self_retrieval": probe_self_retrieval(svc, conn, args.scope, 60, args.seed),
            "nonsense_query": probe_nonsense_query(svc, args.scope),
            "index_false_positives": probe_index_false_positives(
                conn, ["deploy", "release", "backup", "sensitivity"], 300),
            "top1_relevance": probe_top1_relevance(svc, conn, args.scope, 60, args.seed),
        },
    }
    scoreboard["elapsed_seconds"] = round(time.time() - started, 1)
    conn.close()

    if args.freeze:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(scoreboard, indent=2) + "\n", encoding="utf-8")
        print(f"linea base escrita en {BASELINE}")
        return 0

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
