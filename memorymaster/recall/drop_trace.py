"""Por que una claim NO salio en los resultados.

`recall_analysis` explica el ranking de lo que SOBREVIVIO. No dice nada de lo que
se cayo, y esa es justamente la pregunta que uno hace cuando algo falla: "esto
tendria que haber aparecido, donde se perdio?".

El agujero no es teorico. El 2026-08-19 un filtro nuevo empezo a descartar claims
PINEADAS — una intencion explicita del operador — con un `continue` mudo. No hubo
error, ni log, ni diferencia visible en la respuesta: simplemente la claim dejo de
estar. Un filtro que descarta sin dejar rastro es la misma señal inerte que
veniamos cazando, solo que del lado de los datos.

APAGADO POR DEFECTO Y SIN COSTO. El camino caliente hace una lectura de un
threading.local y sigue; no se asigna nada mientras nadie este grabando. Se activa
solo alrededor de una llamada de diagnostico:

    with recording() as drops:
        svc.query_rows(...)
    # drops -> [{"claim_id": 42, "reason": "zero_relevance", ...}, ...]

POR QUE threading.local Y NO UN PARAMETRO. Pasar un acumulador por firma exigiria
atravesar query_rows -> _query_legacy_mode -> rank_claim_rows, tres capas cuyo
unico interes en el dato seria reenviarlo, y service.py esta contra su presupuesto
de tamaño. El costo de esa comodidad es que esto NO cruza hilos: quien grabe en un
hilo no ve descartes de otro. Es la eleccion correcta para diagnostico y la
equivocada para telemetria agregada — para eso esta retrieval_stats.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

_local = threading.local()

# Razones canonicas. Sostenerlas como constantes evita que el motivo se escriba
# distinto en cada sitio y se vuelva imposible de agrupar despues.
ZERO_RELEVANCE = "zero_relevance"
SESSION_DIVERSITY_CAP = "session_diversity_cap"
LIMIT_TRUNCATION = "limit_truncation"


def record(claim_id: int | None, reason: str, **detail: Any) -> None:
    """Anota un descarte si hay una grabacion activa. Si no, no hace nada."""
    sink = getattr(_local, "sink", None)
    if sink is None:
        return
    entry: dict[str, Any] = {"claim_id": claim_id, "reason": reason}
    if detail:
        entry.update(detail)
    sink.append(entry)


@contextmanager
def recording() -> Iterator[list[dict[str, Any]]]:
    """Graba los descartes ocurridos dentro del bloque, en este hilo.

    Anida sin pisar: al salir restaura el sink anterior, asi un diagnostico
    adentro de otro no le roba los eventos al de afuera.
    """
    previous = getattr(_local, "sink", None)
    sink: list[dict[str, Any]] = []
    _local.sink = sink
    try:
        yield sink
    finally:
        _local.sink = previous


def is_recording() -> bool:
    return getattr(_local, "sink", None) is not None


def summarize(drops: list[dict[str, Any]]) -> dict[str, int]:
    """Cuenta por razon. Util cuando la lista es larga y solo importa el patron."""
    out: dict[str, int] = {}
    for d in drops:
        reason = str(d.get("reason", "unknown"))
        out[reason] = out.get(reason, 0) + 1
    return out
