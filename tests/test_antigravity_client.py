"""El cliente de `agy` no puede inventar exito ni colgarse.

Los dos casos que importan estan primero y ninguno es sobre el camino feliz:

  - `agy` puede salir con codigo 0 y traer status FAILED adentro del JSON. Mirar
    solo el returncode dejaria pasar una respuesta fallida como buena, que es la
    forma exacta de bug que este repo estuvo persiguiendo: algo que se lee como
    resultado y no lo es.
  - un timeout tiene que LEVANTAR, no devolver vacio. Una consolidacion que
    "no encontro nada" porque el proveedor no contesto es peor que una que falla.

No se invoca el `agy` real: los tests usan un runner falso. Un test que necesita
red y OAuth no corre en CI, y un test que no corre no protege nada.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from memorymaster.core.antigravity_client import (
    MEASURED_CALL_OVERHEAD_TOKENS,
    AntigravityClient,
    AntigravityError,
    strip_code_fence,
)


def _envelope(*, status="SUCCESS", response="hola", inp=20011, out=42, think=7):
    return json.dumps({
        "conversation_id": "abc-123",
        "status": status,
        "response": response,
        "duration_seconds": 1.5,
        "num_turns": 1,
        "usage": {"input_tokens": inp, "output_tokens": out, "thinking_tokens": think},
    })


def _runner(stdout="", returncode=0, stderr="", raises=None):
    def run(command, prompt, timeout, cwd, env):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    return run


def _client(**kw):
    kw.setdefault("command", "agy")
    kw.setdefault("model", "gemini-3.7-flash-low")
    return AntigravityClient(**kw)


@pytest.fixture(autouse=True)
def _agy_on_path(monkeypatch):
    monkeypatch.setattr(
        "memorymaster.core.antigravity_client.shutil.which", lambda _: "/fake/agy"
    )


# --- lo que no puede pasar ------------------------------------------------

def test_a_failed_status_raises_even_when_the_exit_code_is_zero(tmp_path: Path):
    """El caso sutil: proceso OK, respuesta fallida."""
    c = _client(runner=_runner(_envelope(status="FAILED")), work_dir=tmp_path)
    with pytest.raises(AntigravityError, match="status=FAILED"):
        c.complete("cualquier cosa")


def test_a_timeout_raises_instead_of_returning_empty(tmp_path: Path):
    c = _client(
        runner=_runner(raises=subprocess.TimeoutExpired("agy", 300)),
        work_dir=tmp_path, timeout=300,
    )
    with pytest.raises(AntigravityError, match="no respondio"):
        c.complete("cualquier cosa")


def test_an_unknown_model_surfaces_the_exit_code_and_stderr(tmp_path: Path):
    """`agy` sale 1 ante un modelo desconocido. No se cae a un default en silencio."""
    c = _client(
        model="gemini-inexistente",
        runner=_runner(returncode=1, stderr="unknown model"),
        work_dir=tmp_path,
    )
    with pytest.raises(AntigravityError) as err:
        c.complete("x")
    assert "codigo 1" in str(err.value) and "gemini-inexistente" in str(err.value)


def test_missing_cli_is_reported_not_swallowed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "memorymaster.core.antigravity_client.shutil.which", lambda _: None
    )
    c = _client(runner=_runner(_envelope()), work_dir=tmp_path)
    with pytest.raises(AntigravityError, match="no esta en el PATH"):
        c.complete("x")


def test_empty_or_malformed_output_raises(tmp_path: Path):
    for salida in ("", "no soy json"):
        c = _client(runner=_runner(salida), work_dir=tmp_path)
        with pytest.raises(AntigravityError):
            c.complete("x")


# --- camino feliz y contrato ----------------------------------------------

def test_a_successful_call_reports_usage(tmp_path: Path):
    c = _client(runner=_runner(_envelope(response="listo")), work_dir=tmp_path)
    r = c.complete("x")
    assert r.text == "listo"
    assert r.input_tokens == 20011 and r.output_tokens == 42 and r.thinking_tokens == 7
    assert r.model == "gemini-3.7-flash-low"


def test_the_model_and_json_format_reach_the_command_line(tmp_path: Path):
    visto = {}

    def run(command, prompt, timeout, cwd, env):
        visto["cmd"] = command
        return subprocess.CompletedProcess(command, 0, _envelope(), "")

    _client(runner=run, model="gemini-3.6-flash-low", work_dir=tmp_path).complete("x")
    cmd = visto["cmd"]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gemini-3.6-flash-low"
    assert cmd[cmd.index("--output-format") + 1] == "json", (
        "sin --output-format json no hay envelope que parsear y el status se pierde"
    )


def test_the_measured_overhead_is_recorded(tmp_path: Path):
    """El piso de ~20k por llamada es el argumento entero a favor de agrupar.

    Si alguien lo baja creyendo que es una estimacion conservadora, el calculo de
    cuando conviene agrupar deja de cerrar. Fue medido, no estimado.
    """
    assert MEASURED_CALL_OVERHEAD_TOKENS == 20_000


@pytest.mark.parametrize("crudo,esperado", [
    ("```json\n[1,2]\n```", "[1,2]"),
    ("```\n{\"a\":1}\n```", '{"a":1}'),
    ('{"a":1}', '{"a":1}'),
    ("", ""),
])
def test_strip_code_fence(crudo, esperado):
    assert strip_code_fence(crudo) == esperado
