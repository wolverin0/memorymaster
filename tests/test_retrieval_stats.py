"""El acumulador de recall tiene que estar ENCHUFADO y no poder romper el recall.

Los dos casos que importan estan primero y no son sobre aritmetica:

  1. que query_rows este realmente decorado — un contador sin llamador cuenta
     cero para siempre y su silencio se lee igual que "no hubo consultas". Es la
     falla que se repitio todo el 2026-08-19 y la que ningun test de aritmetica ve.
  2. que un fallo del contador NO tumbe una consulta que funciona. La telemetria
     es estrictamente observacional; si puede romper el recall, es un pasivo.

Lo demas —tasas, denominadores— es aritmetica y va despues.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import retrieval_stats


@pytest.fixture(autouse=True)
def _clean_counters():
    retrieval_stats.get_stats().reset()
    yield
    retrieval_stats.get_stats().reset()


@pytest.fixture()
def svc(tmp_path: Path) -> MemoryService:
    s = MemoryService(tmp_path / "stats.db", workspace_root=tmp_path)
    s.init_db()
    s.ingest(
        text="MemoryMaster stores backups on the E drive and trims them weekly.",
        citations=[CitationInput(source="test", locator="l", excerpt="e")],
        scope="project:test", source_agent="pytest", confidence=0.9,
    )
    return s


def _q(svc, text, **kw):
    return svc.query_rows(
        text, limit=5, scope_allowlist=["project:test"], include_candidates=True,
        record_accesses=False, **kw,
    )


# --- cableado -------------------------------------------------------------

def test_query_rows_is_actually_instrumented(svc):
    """El caso que fallaba en todos los demas hallazgos del dia."""
    _q(svc, "backups drive")
    snap = retrieval_stats.get_stats().snapshot()
    assert snap["sample"] == 1, (
        "query_rows no incremento el contador: el decorador @observed no esta "
        "aplicado, o se aplico a otra funcion. Un acumulador sin llamador "
        "reporta 0 para siempre y eso se lee igual que 'no hubo trafico'."
    )


def test_every_return_path_is_counted(svc):
    """query_rows tiene varios `return`; por eso es decorador y no una llamada suelta.

    El corto de limite <= 0 sale por un return distinto al del camino normal.
    Instrumentar uno solo mide la mitad del trafico.
    """
    _q(svc, "backups drive")
    svc.query_rows("lo que sea", limit=0)
    assert retrieval_stats.get_stats().snapshot()["sample"] == 2


def test_telemetry_failure_cannot_break_recall(svc, monkeypatch):
    """Un contador roto no puede tumbar una consulta que anda."""
    def explota(**kwargs):
        raise RuntimeError("contador roto")

    monkeypatch.setattr(retrieval_stats.get_stats(), "record", explota)
    rows = _q(svc, "backups drive")
    assert rows, "la consulta fallo por culpa de la telemetria"


# --- aritmetica -----------------------------------------------------------

def test_zero_result_rate_counts_the_empty_queries(svc):
    _q(svc, "backups drive")            # devuelve algo
    _q(svc, "xyzzy plugh frobnicate")   # no matchea nada
    snap = retrieval_stats.get_stats().snapshot()
    assert snap["zero_result_queries"] == 1
    assert snap["zero_result_rate"] == 0.5


def test_errors_are_excluded_from_the_rate(svc, monkeypatch):
    """Una consulta que EXPLOTA no es una consulta que no encontro nada.

    Contarla como cero-resultado inflaria la tasa con fallas de sistema y haria
    ver un problema de recall donde hay un problema de codigo.
    """
    _q(svc, "backups drive")

    def revienta(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "_query_legacy_mode", revienta)
    with pytest.raises(RuntimeError):
        _q(svc, "backups drive")

    snap = retrieval_stats.get_stats().snapshot()
    assert snap["errors"] == 1
    assert snap["sample"] == 2
    assert snap["zero_result_rate"] == 0.0, (
        "el error entro al denominador de la tasa de cero-resultados"
    )


def test_rate_is_zero_with_no_traffic():
    """Sin consultas no hay tasa. `sample` es lo que dice si el numero significa algo."""
    snap = retrieval_stats.get_stats().snapshot()
    assert snap["sample"] == 0
    assert snap["zero_result_rate"] == 0.0
