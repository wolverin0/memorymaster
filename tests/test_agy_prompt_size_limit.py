"""Un prompt demasiado largo tiene que decir QUE es demasiado largo.

QUE PASO (medido el 2026-08-25). `agy` recibe el prompt como argumento (`-p`), y
Windows corta la linea de comandos en 32.767 caracteres. Pasado ese punto
CreateProcess falla, Python levanta FileNotFoundError, y el cliente lo traducia a
"no se pudo ejecutar 'agy'" — un mensaje que manda a verificar la instalacion de
un CLI que esta perfectamente instalado.

EL COSTO NO FUE EL MENSAJE, FUE EL TIEMPO. El perfil compilado quedo CINCO DIAS
sin avanzar (watermark clavado, map_calls=0) con `AntigravityError` como unica
pista, porque su lote por defecto era de 200.000 caracteres: seis veces el
limite. El diagnostico correcto exigio bisecar el tamano a mano.

EL TEST QUE IMPORTA ES `test_the_error_names_the_actual_problem`: que corte no
alcanza si el mensaje sigue sin decir por que. Y `test_it_reports_the_number`
existe porque sin el numero hay que volver a bisecar para elegir un lote nuevo.

Umbrales medidos contra el CLI real: 32.014 caracteres pasan, 40.014 no.
"""
from __future__ import annotations

import pytest

from memorymaster.core.antigravity_client import (
    _MAX_PROMPT_CHARS,
    AntigravityClient,
    AntigravityError,
)


def _cliente(monkeypatch, *, invocaciones: list | None = None) -> AntigravityClient:
    """Cliente con el CLI fingido presente: aislamos el limite, no la instalacion."""
    monkeypatch.setattr(AntigravityClient, "available", lambda self: True)

    def _runner(command, entrada, timeout, cwd, env):  # noqa: ANN001, ANN202
        if invocaciones is not None:
            invocaciones.append(command)
        import subprocess

        # Forma real que espera _parse: status SUCCESS y usage anidado. Un exit 0
        # con status distinto se rechaza a proposito, asi que la respuesta falsa
        # tiene que ser exacta o el test mediria el parser en vez del limite.
        return subprocess.CompletedProcess(
            command,
            0,
            '{"status":"SUCCESS","response":"ok",'
            '"usage":{"input_tokens":1,"output_tokens":1}}',
            "",
        )

    return AntigravityClient(runner=_runner)


# --- el corte -------------------------------------------------------------

def test_an_oversized_prompt_never_reaches_the_command_line(monkeypatch):
    invocaciones: list = []
    cliente = _cliente(monkeypatch, invocaciones=invocaciones)

    with pytest.raises(AntigravityError):
        cliente.complete("x" * (_MAX_PROMPT_CHARS + 1))

    assert invocaciones == [], (
        "se intento invocar agy con un prompt sobre el limite; el corte tiene que "
        "ocurrir ANTES, que es lo que evita el FileNotFoundError enganoso"
    )


def test_the_error_names_the_actual_problem(monkeypatch):
    """Contra-caso del bug: el mensaje NO puede sugerir que falta el CLI."""
    cliente = _cliente(monkeypatch)
    with pytest.raises(AntigravityError) as exc:
        cliente.complete("x" * (_MAX_PROMPT_CHARS + 1))

    mensaje = str(exc.value)
    assert "linea de comandos" in mensaje, f"el mensaje no explica la causa: {mensaje}"
    assert "no se pudo ejecutar" not in mensaje, (
        "volvio el mensaje que manda a buscar un CLI que si esta instalado"
    )


def test_it_reports_the_number(monkeypatch):
    """Sin el tamano real no se puede elegir un lote nuevo sin volver a bisecar."""
    cliente = _cliente(monkeypatch)
    largo = _MAX_PROMPT_CHARS + 1234
    with pytest.raises(AntigravityError) as exc:
        cliente.complete("x" * largo)
    assert str(largo) in str(exc.value)


def test_it_points_at_the_knob_that_fixes_it(monkeypatch):
    cliente = _cliente(monkeypatch)
    with pytest.raises(AntigravityError) as exc:
        cliente.complete("x" * (_MAX_PROMPT_CHARS + 1))
    assert "MEMORYMASTER_PROFILE_MAX_INPUT_CHARS" in str(exc.value)


# --- control negativo: por debajo del limite SIGUE funcionando -------------

def test_a_prompt_under_the_limit_still_runs(monkeypatch):
    """Sin esto, un corte puesto en cero pasaria todos los tests de arriba."""
    invocaciones: list = []
    cliente = _cliente(monkeypatch, invocaciones=invocaciones)

    respuesta = cliente.complete("x" * (_MAX_PROMPT_CHARS - 1000))

    assert respuesta.text == "ok"
    assert len(invocaciones) == 1, "el prompt valido no llego al CLI"


def test_the_limit_leaves_room_for_the_rest_of_the_command_line():
    """El resto de la linea (ejecutable, --model, --output-format) tambien cuenta.

    32.767 es el limite duro de Windows; el corte tiene que quedar por debajo,
    no encima, o el margen no sirve para nada.
    """
    assert _MAX_PROMPT_CHARS < 32_767
    assert _MAX_PROMPT_CHARS >= 16_000, (
        "un limite demasiado bajo fragmentaria los lotes sin necesidad"
    )
