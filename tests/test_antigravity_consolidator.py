"""Consolidacion sobre Gemini/OAuth: un lote, una llamada, y sin decisiones faltantes.

El caso que mas importa aca no es que funcione: es
`test_the_whole_batch_goes_in_one_call`. Cada invocacion de `agy` arrastra ~20k
tokens fijos, asi que consolidar N candidatos de a uno costaria N*20k. Ese test
convierte "agrupar" de intencion en contrato — si alguien refactoriza a un bucle
por candidato, el costo se multiplica en silencio y nada mas se lo dice.

El segundo es `test_a_short_answer_is_rejected`: un consolidador que devuelve
menos decisiones que candidatos deja candidatos sin resolver, y un candidato sin
resolver se ve identico a uno ignorado.
"""
from __future__ import annotations

import json

import pytest

from memorymaster.dreaming.providers import (
    AntigravityConsolidator,
    ProviderCallError,
    create_dream_consolidator,
)


class _FakeClient:
    """Cliente falso: cuenta llamadas y devuelve lo que se le indique."""

    def __init__(self, response_text: str = "", raises: Exception | None = None):
        self.response_text = response_text
        self.raises = raises
        self.calls: list[str] = []

    def complete(self, prompt: str):
        self.calls.append(prompt)
        if self.raises is not None:
            raise self.raises
        return type(
            "R", (), {
                "text": self.response_text, "input_tokens": 20011,
                "output_tokens": 120, "thinking_tokens": 9,
                "duration_seconds": 2.0, "conversation_id": "x", "model": "m",
            },
        )()


class _Candidate:
    """Minimo que la consolidacion necesita de un DreamCandidate."""

    def __init__(self, cid: str):
        self.candidate_id = cid

    def to_dict(self):
        return {"candidate_id": self.candidate_id, "text": f"texto {self.candidate_id}"}


def _decisions(*ids):
    return json.dumps({
        "decisions": [
            {"candidate_id": i, "action": "add", "rationale": "r", "confidence": 0.8}
            for i in ids
        ]
    })


def test_the_whole_batch_goes_in_one_call():
    """El contrato de costo. Un bucle por candidato multiplicaria el piso de 20k."""
    cands = [_Candidate(f"c{i}") for i in range(5)]
    client = _FakeClient(_decisions(*[c.candidate_id for c in cands]))
    con = AntigravityConsolidator(client=client, model="gemini-3.7-flash-low")

    con.consolidate(cands, [], scope="project:test")

    assert len(client.calls) == 1, (
        f"se hicieron {len(client.calls)} llamadas para 5 candidatos; cada una "
        "cuesta ~20k tokens de andamiaje fijo, asi que el lote va entero o no va"
    )
    for c in cands:
        assert c.candidate_id in client.calls[0], "el prompt no llevo todos los candidatos"


def test_a_short_answer_is_rejected():
    """Menos decisiones que candidatos deja candidatos sin resolver, indistinguibles de ignorados."""
    cands = [_Candidate("c0"), _Candidate("c1")]
    con = AntigravityConsolidator(client=_FakeClient(_decisions("c0")), model="m")
    with pytest.raises(ProviderCallError, match="exactly one decision"):
        con.consolidate(cands, [], scope="project:test")


def test_an_invented_candidate_id_is_rejected():
    """Un id que el modelo se invento no puede entrar como decision valida.

    Levanta ValueError y no ProviderCallError, y esta bien asi: el worker atrapa
    Exception de forma amplia, y el trato especial de ProviderCallError es solo el
    backoff ante un 429 — que no aplica a un id inventado. Se asevera el
    comportamiento REAL en vez de forzar el codigo a coincidir con la expectativa
    que traia este test.
    """
    cands = [_Candidate("c0")]
    con = AntigravityConsolidator(client=_FakeClient(_decisions("c0", "inventado")), model="m")
    with pytest.raises(ValueError, match="unknown candidate"):
        con.consolidate(cands, [], scope="project:test")


def test_a_markdown_fenced_answer_still_parses():
    """Los modelos ponen el cerco igual aunque se les pida que no."""
    cands = [_Candidate("c0")]
    fenced = "```json\n" + _decisions("c0") + "\n```"
    con = AntigravityConsolidator(client=_FakeClient(fenced), model="m")
    result = con.consolidate(cands, [], scope="project:test")
    assert len(result.decisions) == 1


def test_a_provider_failure_becomes_a_provider_call_error():
    """El worker trata ProviderCallError como reintentable; una excepcion cruda no."""
    from memorymaster.core.antigravity_client import AntigravityError

    con = AntigravityConsolidator(
        client=_FakeClient(raises=AntigravityError("timeout")), model="m"
    )
    with pytest.raises(ProviderCallError, match="timeout"):
        con.consolidate([_Candidate("c0")], [], scope="project:test")


def test_usage_reports_the_antigravity_provider():
    cands = [_Candidate("c0")]
    con = AntigravityConsolidator(client=_FakeClient(_decisions("c0")), model="gemini-3.7-flash-low")
    result = con.consolidate(cands, [], scope="project:test")
    assert result.usage.provider == "antigravity"
    assert result.usage.input_tokens == 20011


# --- la eleccion de proveedor ---------------------------------------------

def test_the_default_consolidator_is_antigravity(monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_DREAM_CONSOLIDATE_PROVIDER", raising=False)
    assert isinstance(create_dream_consolidator(), AntigravityConsolidator)



def test_an_unknown_provider_raises_instead_of_defaulting(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DREAM_CONSOLIDATE_PROVIDER", "typo-provider")
    with pytest.raises(ProviderCallError, match="must select Gemini"):
        create_dream_consolidator()


def test_a_leftover_glm_model_string_does_not_reach_agy(monkeypatch, caplog):
    """El riesgo real de la migracion, encontrado en el entorno del operador.

    MEMORYMASTER_DREAM_CONSOLIDATE_MODEL la compartian ambos consolidadores y las
    instalaciones existentes la tienen con prefijo de proveedor. Pasarsela a `agy`
    lo hace salir con "unknown model" y rompe el dreaming apenas cambia el default.

    Se ignora con WARNING, no en silencio: el valor pertenece a un proveedor dado
    de baja, no es un nombre mal escrito, y el operador ya pidio Gemini.
    """
    import logging

    monkeypatch.setenv("MEMORYMASTER_DREAM_CONSOLIDATE_MODEL", "zai-coding-plan/glm-5.2")
    with caplog.at_level(logging.WARNING):
        con = AntigravityConsolidator(client=_FakeClient(_decisions("c0")))

    assert "/" not in con.model, "un modelo con prefijo de proveedor llego al cliente de agy"
    assert con.model == "gemini-3.7-flash-low"
    assert any("MEMORYMASTER_DREAM_CONSOLIDATE_MODEL" in r.message for r in caplog.records), (
        "se ignoro la variable sin avisar; eso la vuelve indistinguible de una que funciono"
    )


def test_an_explicit_agy_model_is_respected(monkeypatch):
    """Contra-caso: no se pisa una eleccion legitima."""
    monkeypatch.setenv("MEMORYMASTER_DREAM_CONSOLIDATE_MODEL", "gemini-3.6-flash-low")
    con = AntigravityConsolidator(client=_FakeClient(_decisions("c0")))
    assert con.model == "gemini-3.6-flash-low"
