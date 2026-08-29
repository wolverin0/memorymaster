"""Registrar los parametros que los llamadores inventan, antes de prohibirlos.

POR QUE EXISTE. Las 51 herramientas MCP declaran su schema sin
``additionalProperties: false``, asi que un argumento que no existe se descarta
en silencio en vez de rechazarse. Eso produjo dos bugs el 2026-08-21:
``list_claims(ids=...)`` devolvia el listado entero sin filtrar, y
``query_memory(scope=...)`` devolvia todas las claims — en ambos casos porque no
habia filtro que fallara, habia un argumento que se evaporaba.

El arreglo obvio es poner ``additionalProperties: false`` en las 51. El operador
decidio NO hacerlo a ciegas: endurecer de golpe convierte en error lo que hoy se
ignora, y rompe sin aviso a cualquier llamador que venga funcionando por
accidente — incluidos hooks y scripts propios. Primero se mide quien pasa que.

ESTO NO PUEDE SER OTRA SEÑAL INERTE. Un contador que nadie lee tiene el mismo
silencio que "nadie pasa parametros de mas", y esa confusion es exactamente la
familia de bug que este repo viene persiguiendo. Por eso:

  - se registra en un JSONL durable, no solo en memoria, para que reiniciar el
    server no borre la evidencia;
  - hay un lector, ``scripts/unknown_args_report.py``, que es lo que se consulta
    para decidir el endurecimiento;
  - el registro es ADITIVO y nunca altera la llamada: observar no puede cambiar
    lo que se observa, y menos romperlo.
"""
from __future__ import annotations

import json
import os
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "record_unknown_arguments",
    "snapshot",
    "reset",
    "log_path",
]

_LOCK = threading.Lock()
_COUNTS: Counter[tuple[str, str]] = Counter()

_ENV_LOG_PATH = "MEMORYMASTER_UNKNOWN_ARGS_LOG"
_ENV_DISABLED = "MEMORYMASTER_UNKNOWN_ARGS_DISABLED"


def _disabled() -> bool:
    return os.environ.get(_ENV_DISABLED, "").strip().lower() in {"1", "true", "yes", "on"}


def log_path() -> Path | None:
    """Donde se acumula la evidencia durable, o ``None`` si no hay destino.

    Sin destino configurado el registro sigue contando en memoria: perder el
    archivo no debe apagar la medicion, solo acortarla a la vida del proceso.
    """
    raw = os.environ.get(_ENV_LOG_PATH, "").strip()
    return Path(raw) if raw else None


def record_unknown_arguments(
    tool_name: str,
    provided: Iterable[str],
    declared: Iterable[str],
) -> list[str]:
    """Anotar los argumentos provistos que la herramienta no declara.

    Devuelve la lista de desconocidos — vacia en el caso normal. Nunca levanta:
    esto corre en el camino de toda llamada MCP, y una medicion que puede tumbar
    una consulta es peor que no medir.
    """
    try:
        if _disabled():
            return []
        declarados = set(declared)
        desconocidos = sorted(k for k in provided if k not in declarados)
        if not desconocidos:
            return []

        with _LOCK:
            for clave in desconocidos:
                _COUNTS[(tool_name, clave)] += 1

        destino = log_path()
        if destino is not None:
            registro = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "tool": tool_name,
                "unknown": desconocidos,
            }
            try:
                destino.parent.mkdir(parents=True, exist_ok=True)
                with destino.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
            except OSError:
                # El contador en memoria ya quedo. Un disco lleno no puede
                # convertir una observacion en una falla de la herramienta.
                pass
        return desconocidos
    except Exception:  # noqa: BLE001 - ver docstring: observar nunca rompe
        return []


def snapshot() -> dict[str, Any]:
    """Lo acumulado en este proceso, ordenado por frecuencia."""
    with _LOCK:
        filas = [
            {"tool": tool, "argument": arg, "count": n}
            for (tool, arg), n in _COUNTS.most_common()
        ]
        total = sum(_COUNTS.values())
    return {
        "total_unknown_calls": total,
        "distinct_pairs": len(filas),
        "rows": filas,
        "log": str(log_path()) if log_path() else None,
        "disabled": _disabled(),
    }


def reset() -> None:
    with _LOCK:
        _COUNTS.clear()
