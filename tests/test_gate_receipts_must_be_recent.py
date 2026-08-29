"""Un recibo viejo no prueba que la tarea siga viva.

QUE PASABA (verificado el 2026-08-29 sobre bd4f6c5): `checkpoint_result` exigia
que `completed_at` fuera PARSEABLE y nada mas, sin compararlo nunca contra `now`,
y el `if valid: return PASS` cortocircuitaba antes de que la rama never_ran/due
llegara a evaluarse. Una tarea que corrio UNA vez y murio despues reportaba PASS
para siempre.

Es la clase wisp-cron un escalon peor: aquel figuraba sano por AUSENCIA de
chequeo; esto figuraba sano por un chequeo que existia y miraba el campo
equivocado. Un gate que no puede volverse rojo no es un gate.

Las dos ramas, porque una sola no prueba nada: reciente => PASS (si no, el test
pasaria con cualquier cosa que devuelva FAIL) y viejo => FAIL nombrando la causa.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from run_v47_operational_acceptance import (  # noqa: E402
    RECEIPT_MAX_AGE_HOURS,
    Verdict,
    checkpoint_result,
)

TASK = "MemoryMaster-Checkpoint-Daily"
NOW = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)
ESTADO_VIVO = {"enabled": True, "last_run": NOW.isoformat(), "next_run": NOW.isoformat()}


def _recibo(cuando: datetime) -> dict:
    return {"task": TASK, "work_performed": True, "result": "pass",
            "completed_at": cuando.isoformat()}


def test_a_recent_receipt_passes():
    reciente = _recibo(NOW - timedelta(hours=1))
    r = checkpoint_result(TASK, ESTADO_VIVO, [reciente], NOW)
    assert r.verdict is Verdict.PASS


def test_an_old_receipt_no_longer_passes_forever():
    """El caso real: corrio una vez hace semanas y nunca mas."""
    viejo = _recibo(NOW - timedelta(days=30))
    r = checkpoint_result(TASK, ESTADO_VIVO, [viejo], NOW)
    assert r.verdict is Verdict.FAIL, "un recibo de hace un mes seguia dando PASS"
    assert "stale" in r.detail.lower(), "el veredicto tiene que DECIR por que fallo"


def test_the_boundary_is_the_declared_horizon():
    apenas_dentro = _recibo(NOW - timedelta(hours=RECEIPT_MAX_AGE_HOURS - 1))
    apenas_fuera = _recibo(NOW - timedelta(hours=RECEIPT_MAX_AGE_HOURS + 1))
    assert checkpoint_result(TASK, ESTADO_VIVO, [apenas_dentro], NOW).verdict is Verdict.PASS
    assert checkpoint_result(TASK, ESTADO_VIVO, [apenas_fuera], NOW).verdict is Verdict.FAIL


def test_the_sentinel_is_watched():
    """El vigilante tiene que estar entre los vigilados (mm-b234).

    El sentinel de freshness solo comprueba divergencia cuando el mismo logra
    ejecutarse: no puede detectar su propia ausencia. Alguien tiene que mirar su
    Last Run Time, y ese alguien es este gate.
    """
    from run_v47_operational_acceptance import TASK_NAMES

    assert "MM-freshness-sentinel" in TASK_NAMES
