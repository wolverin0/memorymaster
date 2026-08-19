"""Los descartes silenciosos tienen que dejar rastro — y sólo cuando se los pide.

El orden de los casos es deliberado. Primero que esté APAGADO: un rastreador que
graba siempre es una fuga de memoria en el camino caliente, y el modo por defecto
es el que corre millones de veces. Después, que grabe la razón correcta.

El caso que da origen a todo esto es `test_a_pinned_claim_is_never_dropped_silently`:
el 2026-08-19 un filtro nuevo empezó a descartar claims pineadas con un `continue`
mudo, y nadie se enteró. No hubo error ni log; la claim simplemente dejó de estar.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import drop_trace

NONSENSE = "xyzzy plugh frobnicate"


@pytest.fixture()
def svc(tmp_path: Path) -> MemoryService:
    s = MemoryService(tmp_path / "drops.db", workspace_root=tmp_path)
    s.init_db()
    for text in [
        "MemoryMaster stores backups on the E drive and trims them weekly.",
        "The steward promotes candidate claims once corroborated.",
        "Recall ranking orders results by lexical relevance.",
    ]:
        s.ingest(
            text=text, citations=[CitationInput(source="t", locator="l", excerpt="e")],
            scope="project:test", source_agent="pytest", confidence=0.9,
        )
    return s


def _q(svc, text, *, limit=5, **kw):
    return svc.query_rows(
        text, limit=limit, scope_allowlist=["project:test"], include_candidates=True,
        record_accesses=False, **kw,
    )


# --- apagado por defecto --------------------------------------------------

def test_nothing_is_recorded_without_an_active_recording(svc):
    """El camino caliente no paga nada. Si esto falla, el rastreador es un pasivo."""
    assert not drop_trace.is_recording()
    _q(svc, NONSENSE)
    assert not drop_trace.is_recording(), "quedo una grabacion colgada tras la consulta"


def test_recording_is_restored_after_the_block(svc):
    with drop_trace.recording():
        pass
    assert not drop_trace.is_recording()


def test_nested_recordings_do_not_steal_each_others_events(svc):
    """Un diagnostico adentro de otro no puede vaciar al de afuera."""
    with drop_trace.recording() as outer:
        drop_trace.record(1, drop_trace.ZERO_RELEVANCE)
        with drop_trace.recording() as inner:
            drop_trace.record(2, drop_trace.ZERO_RELEVANCE)
        drop_trace.record(3, drop_trace.ZERO_RELEVANCE)

    assert [d["claim_id"] for d in inner] == [2]
    assert [d["claim_id"] for d in outer] == [1, 3]


# --- razones ---------------------------------------------------------------

def test_a_zero_relevance_drop_says_so(svc):
    with drop_trace.recording() as drops:
        rows = _q(svc, NONSENSE, retrieval_mode="hybrid")

    assert rows == []
    assert drop_trace.summarize(drops).get(drop_trace.ZERO_RELEVANCE, 0) > 0, (
        f"la consulta descarto todo y no dejo rastro: {drops}"
    )


def test_a_pinned_claim_is_never_dropped_silently(svc):
    """El caso de origen. Una claim pineada no se descarta — y se puede demostrar.

    Si algun filtro futuro vuelve a descartarla, este test falla por el resultado;
    si la descartara dejando rastro, falla igual, porque una intencion explicita
    del operador no es algo que se resuelva anotandolo en un log.
    """
    claim = svc.ingest(
        text="Completely unrelated statement about turtles.",
        citations=[CitationInput(source="t", locator="lx", excerpt="e")],
        scope="project:test", source_agent="pytest", confidence=0.5,
    )
    svc.store.set_pinned(claim.id, True, "test: intencion explicita del operador")

    with drop_trace.recording() as drops:
        rows = _q(svc, NONSENSE, retrieval_mode="hybrid")

    pinned_drops = [d for d in drops if d["claim_id"] == claim.id]
    assert not pinned_drops, f"la claim pineada fue descartada: {pinned_drops}"
    assert any(r["claim"].id == claim.id for r in rows), (
        "la claim pineada no aparecio en los resultados pese a estar pineada"
    )


def test_truncation_is_distinguishable_from_no_match(svc):
    """"No entro por limite" y "no matcheo" se ven igual desde afuera y no son lo mismo.

    Sin este rastro, una claim que matcheo bien pero quedo en el puesto 6 se lee
    como si no existiera, y se busca el problema en el indice en vez de en el limite.
    """
    with drop_trace.recording() as drops:
        _q(svc, "claims", limit=1)

    reasons = drop_trace.summarize(drops)
    assert reasons.get(drop_trace.LIMIT_TRUNCATION, 0) > 0 or not drops, (
        f"hubo descartes pero ninguno atribuido a truncacion: {reasons}"
    )
