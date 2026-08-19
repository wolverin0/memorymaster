"""Una consulta que no matchea nada no puede devolver resultados con autoridad.

El modo hibrido devolvia cinco claims para una consulta de tokens inexistentes,
cada una con lexical_score 0.000 y score total 0,68, porque el score es una suma
ponderada donde confianza y frescura alcanzan solas para pasar el corte.

La causa es una guarda que se apaga justo cuando hace falta. El floor gate suprime
los bonos de un candidato flojo RELATIVO al mejor:

    max_relevance = max(p.relevance for ...)
    gated = floor_ratio > 0 and max_relevance > 0 and parts.relevance < floor

Cuando nada matchea, max_relevance es 0, la condicion max_relevance > 0 falla, y
ningun candidato queda gateado: todos conservan sus bonos intactos. La guarda
protege contra un mal resultado entre buenos, y se desactiva sola cuando TODOS son
malos — que es el caso en que devolver algo es peor.

Por que importa: quien consulta no distingue "no hay nada sobre esto" de "aca hay
cinco claims", y las cinco vienen con confianza 1.0. Es una respuesta segura de si
misma construida sobre cero evidencia.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService

# Tokens que no pueden existir en ningun corpus.
NONSENSE = "xyzzy plugh frobnicate"


@pytest.fixture()
def service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "hybrid.db", workspace_root=tmp_path)
    svc.init_db()
    for i, text in enumerate([
        "MemoryMaster backups are stored on the E drive and trimmed weekly.",
        "The steward promotes candidate claims once corroborated.",
        "Recall ranking orders results by lexical relevance.",
    ]):
        svc.ingest(
            text=text,
            citations=[CitationInput(source="test", locator=f"l{i}", excerpt="e")],
            scope="project:test", source_agent="pytest", confidence=0.99,
        )
    return svc


def _rows(svc, query, mode, **kw):
    return svc.query_rows(
        query, limit=5, scope_allowlist=["project:test"], include_candidates=True,
        retrieval_mode=mode, record_accesses=False, **kw,
    )


@pytest.mark.parametrize("mode", ["legacy", "hybrid"])
def test_nonsense_query_returns_nothing(service, mode):
    """El caso reportado. legacy ya cumplia; hybrid devolvia cinco."""
    rows = _rows(service, NONSENSE, mode)
    assert rows == [], (
        f"modo {mode} devolvio {len(rows)} resultados para una consulta sin "
        f"ninguna coincidencia: "
        f"{[(r['claim'].id, r['lexical_score'], round(r['score'], 3)) for r in rows]}"
    )


@pytest.mark.parametrize("mode", ["legacy", "hybrid"])
def test_a_real_query_still_returns(service, mode):
    """Contra-metrica: no se gana devolviendo nada nunca."""
    rows = _rows(service, "backups drive", mode)
    assert rows, f"modo {mode} dejo de responder una consulta legitima"
    assert any("backups" in r["claim"].text.lower() for r in rows)


def test_high_confidence_cannot_carry_a_zero_relevance_row(service):
    """La causa raiz, aislada: confianza y frescura no pueden ser toda la relevancia.

    Una claim recien ingerida con confianza maxima es el peor caso — frescura 1.0 y
    confianza 0.99 — y aun asi no debe aparecer si no matchea la consulta.
    """
    service.ingest(
        text="Completely unrelated statement about turtles.",
        citations=[CitationInput(source="test", locator="lx", excerpt="e")],
        scope="project:test", source_agent="pytest", confidence=1.0,
    )
    rows = _rows(service, NONSENSE, "hybrid")
    assert rows == [], (
        "una claim con confianza 1.0 y frescura maxima entro sin relevancia alguna"
    )


def test_partial_match_still_ranks(service):
    """Relevancia baja NO es relevancia cero: un match parcial sigue siendo valido."""
    rows = _rows(service, "steward turtles", "hybrid")
    assert rows, "un match parcial legitimo fue descartado junto con el ruido"
    assert all(r["lexical_score"] > 0 for r in rows)
