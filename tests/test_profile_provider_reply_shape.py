"""Forma de la respuesta del proveedor del perfil compilado (T-1420).

Por que existe: el run 10 del perfil compilado, sobre una copia de la base del
2026-10-30, fallo 2 de 2 veces en el mismo lote con `profile provider returned
malformed JSON`. `agy` contesto el JSON valido dentro de un cerco ```json y
despues agrego `<!-- GOAL_COMPLETE -->`. El parser tiraba solo la ULTIMA linea
como si fuera el cierre del cerco, el cierre real quedaba dentro del texto y el
lote entero se perdia. El requisito: el contenido del cerco se valida igual que
siempre, y lo que hay fuera de el no lo invalida; un texto roto sigue roto.

En las corridas siguientes el modelo copio los nombres que el prompt le mostraba
(`volatilities` del CONTRACT, `candidate_id` escalar del texto) porque el prompt
nunca mostraba la fila que el parser lee. Los prompts ahora la muestran, y una
fila con claves equivocadas se sigue rechazando.
"""
from __future__ import annotations

import json

import pytest

from memorymaster.profile.models import (
    MAX_CANDIDATE_SUPPORTS,
    ProfileMessage,
    ProfileValidationError,
)
from memorymaster.profile.providers import (
    MAP_ROW_SHAPE,
    REDUCE_ROW_SHAPE,
    ProfileMapper,
    ProfileReducer,
    parse_map_output,
    parse_reduce_output,
)

_MESSAGES = (ProfileMessage(3, "s1", "user", "I live in Argentina.", ""),)
_CANDIDATE = {
    "category": "identity_locale",
    "predicate": "location",
    "value": "Argentina",
    "volatility": "stable",
    "support_ids": [3],
}
_DECISION = {
    "action": "add",
    "category": "identity_locale",
    "predicate": "location",
    "value": "Argentina",
    "volatility": "stable",
    "confidence": 0.9,
    "rationale": "stated directly",
}


def _fenced(payload: dict, trailer: str) -> str:
    return "```json\n" + json.dumps(payload, indent=2) + "\n```" + trailer


def _candidates():
    return parse_map_output(json.dumps({"candidates": [_CANDIDATE]}), _MESSAGES)


@pytest.mark.parametrize("trailer", ["\n\n<!-- GOAL_COMPLETE -->", "\nDone.\n", ""])
def test_text_after_the_closing_fence_does_not_reject_the_batch(trailer):
    candidates = parse_map_output(_fenced({"candidates": [_CANDIDATE]}, trailer), _MESSAGES)

    assert [(item.predicate, item.value) for item in candidates] == [("location", "Argentina")]


def test_the_reduce_stage_reads_the_same_shape():
    candidates = _candidates()
    reply = _fenced(
        {"decisions": [{**_DECISION, "candidate_ids": [candidates[0].candidate_id]}]},
        "\n\n<!-- GOAL_COMPLETE -->",
    )

    assert [item.action for item in parse_reduce_output(reply, candidates, ())] == ["add"]


def test_broken_json_inside_the_fence_is_still_malformed():
    raw = '```json\n{"candidates": [\n```\n\n<!-- GOAL_COMPLETE -->'

    with pytest.raises(ProfileValidationError, match="malformed JSON"):
        parse_map_output(raw, _MESSAGES)


def test_an_unclosed_fence_still_reads_its_body():
    assert parse_map_output('```json\n{"candidates": []}', _MESSAGES) == ()


def test_prompts_show_the_exact_row_keys_the_parsers_read():
    """Without a row shape the model copied `volatilities` and a scalar `candidate_id`."""
    assert json.dumps(MAP_ROW_SHAPE, ensure_ascii=False) in ProfileMapper._prompt(_MESSAGES)
    assert json.dumps(REDUCE_ROW_SHAPE, ensure_ascii=False) in ProfileReducer._prompt((), ())
    # The shown keys are the keys a valid row has, and a valid row parses.
    assert set(MAP_ROW_SHAPE) == set(_CANDIDATE)
    candidates = _candidates()
    row = {**_DECISION, "candidate_ids": [candidates[0].candidate_id]}
    assert set(row) <= set(REDUCE_ROW_SHAPE)
    assert isinstance(REDUCE_ROW_SHAPE["candidate_ids"], list)  # never a scalar
    assert parse_reduce_output(json.dumps({"decisions": [row]}), candidates, ())


def test_the_map_prompt_states_the_support_cap_the_parser_enforces():
    """A row citing 39 supports failed its whole batch: the cap was never in the prompt."""
    assert f"1 to {MAX_CANDIDATE_SUPPORTS} supplied message_id" in ProfileMapper._prompt(_MESSAGES)


def test_a_row_past_the_support_cap_is_still_rejected():
    row = {**_CANDIDATE, "support_ids": list(range(-1, -MAX_CANDIDATE_SUPPORTS - 2, -1))}
    messages = tuple(ProfileMessage(i, "s1", "user", "x", "") for i in row["support_ids"])

    with pytest.raises(ProfileValidationError, match="supports are malformed"):
        parse_map_output(json.dumps({"candidates": [row]}), messages)


@pytest.mark.parametrize("bad_row", [
    {key: value for key, value in _CANDIDATE.items() if key != "volatility"} | {"volatilities": "stable"},
    {"category": "working_style", "workflow_preference": "x", "volatility": "stable", "support_ids": [3]},
])
def test_a_row_with_the_wrong_keys_is_still_rejected(bad_row):
    with pytest.raises(ProfileValidationError, match="field .* is invalid"):
        parse_map_output(json.dumps({"candidates": [bad_row]}), _MESSAGES)


def test_a_scalar_candidate_id_is_still_rejected():
    candidates = _candidates()
    row = {**_DECISION, "candidate_id": candidates[0].candidate_id}

    with pytest.raises(ProfileValidationError, match="candidate ids are invalid"):
        parse_reduce_output(json.dumps({"decisions": [row]}), candidates, ())
