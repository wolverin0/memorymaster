"""Marcar que candidato de perfil ya fue consumido por una decision.

El reduce pasaba TODOS los candidatos de un run en una sola llamada, y el
validador exige que el modelo devuelva una particion perfecta: cada
candidate_id exactamente una vez, sin duplicar ni omitir. Eso escala hasta
donde el modelo puede sostener la particion y no mas: el run 2 completo con 68
candidatos, el run 3 acumulo 234 y quedo clavado en `reducing` desde el
2026-08-20 — diez dias de intentos programados, mas cuatro reintentos medidos a
mano, todos fallando entre `profile candidates must appear exactly once` y
`profile provider returned malformed JSON`.

Lotear el reduce es la salida, pero sin esta columna es peor que el problema:
`apply_decisions` relee `candidates(run_id)` entero en cada llamada, asi que un
lote aplicado y un crash antes del siguiente dejaba los mismos candidatos listos
para aplicarse de nuevo — hechos duplicados en el perfil que se inyecta en cada
sesion. `consumed_at` es lo que hace que un run a medias sea reanudable en vez
de destructivo.

Se marca dentro de la MISMA transaccion que aplica la decision. Si eso se
separa, vuelve la doble aplicacion por otra puerta.
"""

from __future__ import annotations

from typing import Any


VERSION = 23
DESCRIPTION = "Track which profile candidates a reduce batch already consumed"


def _columns(conn: Any) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(compiled_profile_candidates)")
    }


def apply_sqlite(conn: Any) -> None:
    if "consumed_at" not in _columns(conn):
        conn.execute(
            "ALTER TABLE compiled_profile_candidates ADD COLUMN consumed_at TEXT"
        )
    # Las filas previas quedan NULL a proposito. Un run ya completado no vuelve a
    # reducirse (su estado terminal lo frena antes), y un run en vuelo DEBE ver
    # sus candidatos como pendientes: son exactamente los que faltan aplicar.
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_compiled_profile_candidates_pending
           ON compiled_profile_candidates(run_id, consumed_at)"""
    )
    conn.commit()


def apply_postgres(conn: Any) -> None:
    """Fail closed: el perfil compilado es SQLite-only, igual que la 21."""
    raise RuntimeError("migration 23 is SQLite-only; the compiled profile is SQLite-only")
