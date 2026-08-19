"""Contadores agregados de recall sobre el trafico REAL.

POR QUE HACE FALTA ALGO MAS QUE LAS SONDAS. El marcador de scripts/probe_suite.py
mide una cohorte CONGELADA de 300 claims: es reproducible y no se puede inflar
eligiendo semilla, y por eso mismo no sabe nada de lo que el operador realmente
pregunta. Si la relevancia sube en la cohorte mientras las consultas reales
empiezan a devolver vacio, las dos mediciones son correctas y el sistema empeoro.

``zero_result_rate`` es la contra-metrica que a las metas del marcador les falta:
la cohorte responde "cuando busco algo que SE que esta, aparece?", y esto responde
"cuando el operador busca, encuentra algo?". Son preguntas distintas y ninguna
sustituye a la otra.

Idea tomada de OpenViking (volcengine, AGPL-3.0) — reimplementada, no copiada:
MemoryMaster es MIT y no puede incorporar codigo AGPL. Ver
artifacts/2026-08-19-openviking-assessment.html.

DOS REGLAS QUE ESTE MODULO NO PUEDE ROMPER:

1. La telemetria NUNCA rompe el recall. Todo lo que se registra va dentro de un
   try/except que se traga cualquier fallo: una consulta que funciona no puede
   fallar porque el contador tuvo un problema. Es lo contrario de la regla normal
   sobre no tragar excepciones, y aplica solo aca porque este codigo es
   ESTRICTAMENTE observacional — no participa de ninguna decision.
2. Nada de I/O en el camino caliente. Son enteros en memoria bajo un lock; quien
   quiera durabilidad llama a ``snapshot()`` y la persiste donde corresponda.
   No se toca el esquema de la DB.
"""
from __future__ import annotations

import functools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# Consultas mas lentas que esto se cuentan aparte. No es un SLA: es el umbral por
# encima del cual una recall deja de sentirse instantanea en un hook interactivo.
SLOW_QUERY_MS = 1000.0


@dataclass
class _Counters:
    queries: int = 0
    results: int = 0
    zero_result_queries: int = 0
    slow_queries: int = 0
    errors: int = 0
    latency_ms_total: float = 0.0
    latency_ms_max: float = 0.0
    by_mode: dict[str, int] = field(default_factory=dict)
    zero_by_mode: dict[str, int] = field(default_factory=dict)


class RetrievalStats:
    """Acumulador thread-safe. Monotonico dentro de la vida del proceso."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._c = _Counters()

    def record(
        self,
        *,
        n_results: int,
        latency_ms: float,
        mode: str = "legacy",
        error: bool = False,
    ) -> None:
        with self._lock:
            c = self._c
            c.queries += 1
            c.by_mode[mode] = c.by_mode.get(mode, 0) + 1
            if error:
                c.errors += 1
                return
            c.results += n_results
            if n_results == 0:
                c.zero_result_queries += 1
                c.zero_by_mode[mode] = c.zero_by_mode.get(mode, 0) + 1
            c.latency_ms_total += latency_ms
            if latency_ms > c.latency_ms_max:
                c.latency_ms_max = latency_ms
            if latency_ms > SLOW_QUERY_MS:
                c.slow_queries += 1

    def snapshot(self) -> dict[str, Any]:
        """Lectura consistente. `sample` primero: una tasa sobre 3 consultas no es una tasa."""
        with self._lock:
            c = self._c
            n = c.queries
            scored = n - c.errors
            return {
                "sample": n,
                "zero_result_rate": round(c.zero_result_queries / scored, 4) if scored else 0.0,
                "zero_result_queries": c.zero_result_queries,
                "mean_results_per_query": round(c.results / scored, 2) if scored else 0.0,
                "mean_latency_ms": round(c.latency_ms_total / scored, 1) if scored else 0.0,
                "max_latency_ms": round(c.latency_ms_max, 1),
                "slow_queries": c.slow_queries,
                "errors": c.errors,
                "by_mode": dict(c.by_mode),
                "zero_by_mode": dict(c.zero_by_mode),
            }

    def reset(self) -> None:
        with self._lock:
            self._c = _Counters()


_STATS = RetrievalStats()


def get_stats() -> RetrievalStats:
    return _STATS


def observed(mode_arg: str = "retrieval_mode") -> Callable:
    """Envuelve un metodo de recall y registra su resultado.

    Decorador y no llamadas sueltas porque ``query_rows`` tiene varios ``return``
    y un contador puesto en uno solo mide la mitad del trafico — precisamente la
    clase de medicion incompleta que este modulo existe para evitar.
    """

    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                rows = fn(*args, **kwargs)
            except Exception:
                try:
                    _STATS.record(
                        n_results=0,
                        latency_ms=(time.perf_counter() - t0) * 1000.0,
                        mode=str(kwargs.get(mode_arg, "legacy")),
                        error=True,
                    )
                except Exception:
                    pass
                raise
            try:
                _STATS.record(
                    n_results=len(rows) if rows is not None else 0,
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                    mode=str(kwargs.get(mode_arg, "legacy")),
                )
            except Exception:
                pass  # la telemetria no rompe el recall
            return rows

        return wrapper

    return decorate
