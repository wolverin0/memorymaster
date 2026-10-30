"""Un JSON con cerco markdown seguido de texto no es JSON malformado (T-1420).

Por que existe: el run 10 del perfil compilado, sobre una copia de la base del
2026-10-30, fallo 2 de 2 veces en el mismo lote con `profile provider returned
malformed JSON`. `agy` contesto el JSON valido dentro de un cerco ```json y
despues agrego `<!-- GOAL_COMPLETE -->`. El parser tiraba solo la ULTIMA linea
como si fuera el cierre del cerco, el cierre real quedaba dentro del texto y el
lote entero se perdia. El requisito: el contenido del cerco se valida igual que
siempre, y lo que hay fuera de el no lo invalida; un texto roto sigue roto.
"""
from __future__ import annotations

import json

import pytest

from memorymaster.profile.models import ProfileMessage, ProfileValidationError
from memorymaster.profile.providers import parse_map_output, parse_reduce_output

_MESSAGES = (ProfileMessage(3, "s1", "user", "I live in Argentina.", ""),)
_CANDIDATE = {
    "category": "identity_locale",
    "predicate": "location",
    "value": "Argentina",
    "volatility": "stable",
    "support_ids": [3],
}


def _fenced(payload: dict, trailer: str) -> str:
    return "```json\n" + json.dumps(payload, indent=2) + "\n```" + trailer


@pytest.mark.parametrize("trailer", ["\n\n<!-- GOAL_COMPLETE -->", "\nDone.\n", ""])
def test_text_after_the_closing_fence_does_not_reject_the_batch(trailer):
    candidates = parse_map_output(_fenced({"candidates": [_CANDIDATE]}, trailer), _MESSAGES)

    assert [(item.predicate, item.value) for item in candidates] == [("location", "Argentina")]


def test_the_reduce_stage_reads_the_same_shape():
    candidates = parse_map_output(json.dumps({"candidates": [_CANDIDATE]}), _MESSAGES)
    reply = _fenced(
        {"decisions": [{
            "candidate_ids": [candidates[0].candidate_id], "action": "add",
            "category": "identity_locale", "predicate": "location", "value": "Argentina",
            "volatility": "stable", "confidence": 0.9, "rationale": "stated directly",
        }]},
        "\n\n<!-- GOAL_COMPLETE -->",
    )

    assert [item.action for item in parse_reduce_output(reply, candidates, ())] == ["add"]


def test_broken_json_inside_the_fence_is_still_malformed():
    raw = '```json\n{"candidates": [\n```\n\n<!-- GOAL_COMPLETE -->'

    with pytest.raises(ProfileValidationError, match="malformed JSON"):
        parse_map_output(raw, _MESSAGES)


def test_an_unclosed_fence_still_reads_its_body():
    assert parse_map_output('```json\n{"candidates": []}', _MESSAGES) == ()
