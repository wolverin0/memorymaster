"""`rerank_with_llm`: el reordenador nunca puede empeorar el recall.

POR QUE EXISTE. De 31 sugerencias de Jules sobre este repo (2026-08-25), esta fue
la unica real: `rerank_with_llm` no tenia NI UNA invocacion en toda la suite.
Verificado contando llamadas, no menciones — las otras diez "faltan tests" que
Jules reporto si estaban cubiertas, algunas con siete invocaciones.

QUE SE PRUEBA, Y POR QUE ESO Y NO OTRA COSA. Esta funcion llama a un juez LLM y
su contrato es que CUALQUIER falla degrada al orden de entrada, nunca a lista
vacia ni a excepcion. Un reordenador que rompe deja al usuario sin recall; uno
que devuelve el orden original solo deja de mejorarlo. Por eso el grueso de los
casos son fallas: excepcion del juez, respuesta sin puntajes parseables, y el
corta-circuito por fallas consecutivas.

`test_a_successful_rerank_actually_reorders` es el control positivo. Sin el,
una funcion que SIEMPRE devolviera `candidates[:top_k]` pasaria todos los casos
de falla y el archivo entero seria decorativo.

El estado global (_DISABLED, _CONSECUTIVE_FAILURES, _STATS) se restaura en un
fixture autouse: sin eso el orden de los tests decide el resultado, que es la
peor clase de test intermitente.
"""
from __future__ import annotations

import pytest

from memorymaster.recall import llm_rerank
from memorymaster.recall.llm_rerank import (
    get_rerank_stats,
    rerank_temporarily_disabled,
    rerank_with_llm,
)


@pytest.fixture(autouse=True)
def _estado_limpio(monkeypatch):
    monkeypatch.setattr(llm_rerank, "_DISABLED", False, raising=False)
    monkeypatch.setattr(llm_rerank, "_CONSECUTIVE_FAILURES", 0, raising=False)
    monkeypatch.setattr(
        llm_rerank,
        "_STATS",
        {"attempts": 0, "successes": 0, "failures": 0, "disabled_fallbacks": 0},
        raising=False,
    )
    yield


CANDIDATOS = ["alfa", "beta", "gamma", "delta"]


def _juez(respuesta):
    """Sustituye la llamada real al LLM; el transporte no es lo que se prueba."""
    def _fake(query, candidates, model):  # noqa: ANN001, ANN202
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta
    return _fake


# --- entradas degeneradas: no deben llamar al juez ------------------------

def test_top_k_zero_returns_empty():
    assert rerank_with_llm("consulta", CANDIDATOS, top_k=0) == []


def test_an_empty_query_short_circuits(monkeypatch):
    llamadas: list = []
    monkeypatch.setattr(
        llm_rerank, "_call_rerank_judge",
        lambda *a: llamadas.append(a) or "",
    )
    assert rerank_with_llm("   ", CANDIDATOS, top_k=2) == CANDIDATOS[:2]
    assert llamadas == [], "se pago una llamada al LLM para una consulta vacia"


def test_no_candidates_short_circuits():
    assert rerank_with_llm("consulta", [], top_k=3) == []


# --- el contrato central: fallar nunca es devolver menos que el orden de entrada

def test_a_judge_exception_falls_back_to_input_order(monkeypatch):
    monkeypatch.setattr(
        llm_rerank, "_call_rerank_judge", _juez(RuntimeError("el juez exploto")),
    )
    assert rerank_with_llm("consulta", CANDIDATOS, top_k=3) == CANDIDATOS[:3]
    assert get_rerank_stats()["failures"] == 1


def test_unparseable_scores_fall_back_to_input_order(monkeypatch):
    monkeypatch.setattr(llm_rerank, "_call_rerank_judge", _juez("no soy json"))
    assert rerank_with_llm("consulta", CANDIDATOS, top_k=3) == CANDIDATOS[:3]
    assert get_rerank_stats()["failures"] == 1


# --- corta-circuito: no insistir contra un juez caido --------------------

def test_repeated_failures_disable_the_reranker(monkeypatch):
    monkeypatch.setattr(llm_rerank, "_max_failures_before_disable", lambda: 2)
    monkeypatch.setattr(
        llm_rerank, "_call_rerank_judge", _juez(RuntimeError("caido")),
    )

    assert rerank_temporarily_disabled() is False
    rerank_with_llm("consulta", CANDIDATOS, top_k=2)
    assert rerank_temporarily_disabled() is False, "se apago con una sola falla"
    rerank_with_llm("consulta", CANDIDATOS, top_k=2)
    assert rerank_temporarily_disabled() is True


def test_once_disabled_the_judge_is_not_called_again(monkeypatch):
    """El punto del corta-circuito es dejar de PAGAR llamadas, no solo de fallar."""
    monkeypatch.setattr(llm_rerank, "_max_failures_before_disable", lambda: 1)
    llamadas: list = []

    def _contar(query, candidates, model):  # noqa: ANN001, ANN202
        llamadas.append(query)
        raise RuntimeError("caido")

    monkeypatch.setattr(llm_rerank, "_call_rerank_judge", _contar)
    rerank_with_llm("consulta", CANDIDATOS, top_k=2)
    assert len(llamadas) == 1

    resultado = rerank_with_llm("consulta", CANDIDATOS, top_k=2)
    assert len(llamadas) == 1, "se llamo al juez con el reordenador ya apagado"
    assert resultado == CANDIDATOS[:2]
    assert get_rerank_stats()["disabled_fallbacks"] == 1


# --- control positivo: cuando el juez anda, SI reordena ------------------

def test_a_successful_rerank_actually_reorders(monkeypatch):
    """Sin este caso, devolver siempre el orden de entrada pasaria todo lo de arriba."""
    monkeypatch.setattr(
        llm_rerank, "_parse_scores",
        lambda respuesta, n: {0: 0.1, 1: 0.9, 2: 0.5, 3: 0.2},
    )
    monkeypatch.setattr(llm_rerank, "_call_rerank_judge", _juez("puntajes"))

    resultado = rerank_with_llm("consulta", CANDIDATOS, top_k=3)

    assert resultado == ["beta", "gamma", "delta"], (
        f"no reordeno por puntaje descendente: {resultado}"
    )
    assert get_rerank_stats()["successes"] == 1


def test_success_resets_the_failure_streak(monkeypatch):
    """Una racha que no se limpia apaga el reordenador por fallas viejas."""
    monkeypatch.setattr(llm_rerank, "_max_failures_before_disable", lambda: 3)
    monkeypatch.setattr(
        llm_rerank, "_call_rerank_judge", _juez(RuntimeError("caido")),
    )
    rerank_with_llm("consulta", CANDIDATOS, top_k=2)
    rerank_with_llm("consulta", CANDIDATOS, top_k=2)
    assert llm_rerank._CONSECUTIVE_FAILURES == 2

    monkeypatch.setattr(llm_rerank, "_parse_scores", lambda r, n: {0: 1.0})
    monkeypatch.setattr(llm_rerank, "_call_rerank_judge", _juez("ok"))
    rerank_with_llm("consulta", CANDIDATOS, top_k=2)

    assert llm_rerank._CONSECUTIVE_FAILURES == 0
    assert rerank_temporarily_disabled() is False


def test_it_never_returns_more_than_top_k(monkeypatch):
    monkeypatch.setattr(llm_rerank, "_parse_scores", lambda r, n: {i: 1.0 for i in range(n)})
    monkeypatch.setattr(llm_rerank, "_call_rerank_judge", _juez("ok"))
    assert len(rerank_with_llm("consulta", CANDIDATOS, top_k=2)) == 2
