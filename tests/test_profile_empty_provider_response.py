"""Una respuesta VACIA del proveedor no es JSON malformado ni un lote perdido (T-1420).

Por que existe: el run 7 del perfil compilado fallo con `profile provider
returned malformed JSON`. El modelo (`agy` con gemini-3.8-flash-low) decidia
llamar una herramienta (`run_command`) en vez de contestar; en modo headless el
permiso se deniega solo y `agy` devuelve `status=SUCCESS` con `response=''`.
`_json_object('')` fallaba entonces con un mensaje que apuntaba al formato, no
a la causa. Medido con datos sinteticos: 5 de 8 llamadas vacias sin directiva,
0 de 8 con ella.

Los tests anclan el requisito: una respuesta vacia se reintenta de forma
acotada, un vacio persistente se reporta como vacio (no como JSON), y la
validacion NO se relaja: un JSON roto con texto sigue fallando sin reintento.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from memorymaster.profile.models import ProfileMessage, ProfileValidationError
from memorymaster.profile.providers import ProfileMapper, ProfileReducer

_GOOD_MAP = json.dumps({"candidates": []})
_MESSAGES = (ProfileMessage(3, "s1", "user", "I live in Argentina.", ""),)


class _ScriptedClient:
    """Cliente falso sin red: devuelve los textos en orden y cuenta las llamadas."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.prompts: list[str] = []

    def complete(self, prompt: str):
        self.prompts.append(prompt)
        return SimpleNamespace(text=self.texts.pop(0) if len(self.texts) > 1 else self.texts[0])


def test_an_empty_response_is_retried_and_a_later_answer_is_used():
    client = _ScriptedClient("", "  \n", _GOOD_MAP)

    assert ProfileMapper(client=client).map(_MESSAGES) == ()
    assert len(client.prompts) == 3


def test_the_reducer_also_retries_an_empty_response():
    client = _ScriptedClient("", json.dumps({"decisions": []}))

    assert ProfileReducer(client=client).reduce((), ()) == ()
    assert len(client.prompts) == 2


def test_a_persistently_empty_response_is_reported_as_empty_not_as_malformed_json():
    client = _ScriptedClient("")

    with pytest.raises(ProfileValidationError, match="empty response") as caught:
        ProfileMapper(client=client).map(_MESSAGES)

    assert "malformed" not in str(caught.value)
    assert 1 < len(client.prompts) <= 5  # acotado: nunca un bucle abierto


def test_malformed_text_is_still_rejected_without_retry():
    client = _ScriptedClient("here is your profile: {not json", _GOOD_MAP)

    with pytest.raises(ProfileValidationError, match="malformed JSON"):
        ProfileMapper(client=client).map(_MESSAGES)

    assert len(client.prompts) == 1


@pytest.mark.parametrize("build", [
    lambda: ProfileMapper._prompt(_MESSAGES),
    lambda: ProfileReducer._prompt((), ()),
])
def test_prompts_forbid_tool_calls_because_headless_agy_denies_them_and_returns_nothing(build):
    prompt = build()

    assert "Do not use tools" in prompt
    assert prompt.index("Do not use tools") < prompt.index("Output JSON only")
