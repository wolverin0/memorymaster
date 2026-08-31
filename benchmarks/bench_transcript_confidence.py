"""Micro-benchmark del costo por llamada de `_transcript_confidence`.

Aisla el mecanismo que rompio el SLO de `cycle_p95` en el PR #248: la funcion
se llama DENTRO de un bucle de minado y, desde que el registro de linaje se
puso antes del gate de bootstrap, abre y cierra una conexion SQLite en cada
iteracion. Antes del cambio, con bootstrap deshabilitado —el default— la
funcion retornaba sin tocar la base.

Mide el camino real, no un proxy: mismo servicio, misma base en disco, mismas
kwargs de linaje. Correr antes y despues del arreglo.

Uso: python benchmarks/bench_transcript_confidence.py [--calls 200]
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memorymaster.core.service import MemoryService  # noqa: E402
from memorymaster.knowledge import rule_miner  # noqa: E402

LINAJE = dict(
    scope="project:bench",
    provider="google",
    source_ref="transcript:bench",
    evidence_hash="e" * 64,
    session_kind="human",
)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=200)
    args = ap.parse_args(argv[1:])

    tmp = Path(tempfile.mkdtemp())
    svc = MemoryService(tmp / "bench.db", workspace_root=tmp)
    svc.init_db()
    rule = {"trigger": "cuando falle el deploy", "action": "revisar el log"}

    # calentar: la primera llamada paga imports y creacion de tabla
    rule_miner._transcript_confidence(svc, rule, root_session_id="warm", **LINAJE)

    inicio = time.perf_counter()
    for n in range(args.calls):
        # raiz distinta por llamada: el caso real de un lote de correcciones,
        # y evita que el dedup por (provider, root) haga el trabajo trivial
        rule_miner._transcript_confidence(
            svc, rule, root_session_id=f"root-{n}", **LINAJE
        )
    total = time.perf_counter() - inicio

    print(f"llamadas      : {args.calls}")
    print(f"total         : {total:.3f}s")
    print(f"por llamada   : {total / args.calls * 1000:.3f} ms")
    print(f"llamadas/seg  : {args.calls / total:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
