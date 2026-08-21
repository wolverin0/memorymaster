"""Medir los parametros inventados sin cambiar lo que se mide.

CONTEXTO. Las 51 herramientas MCP declaran su schema sin
`additionalProperties: false`, asi que un argumento inexistente se descarta en
silencio. Eso causo dos bugs el 2026-08-21 — `list_claims(ids=...)` devolviendo
todo sin filtrar y `query_memory(scope=...)` ignorando el scope. El operador
decidio medir antes de endurecer, porque endurecer a ciegas rompe a quien hoy
funciona por accidente.

LOS DOS TESTS QUE IMPORTAN NO SON SOBRE EL CONTEO. Son
`test_recording_never_breaks_the_call` y `test_a_broken_log_destination_does_not_break_the_call`:
una medicion que puede tumbar una llamada MCP es peor que no medir. El resto
verifica que efectivamente registra, porque un contador que no cuenta tiene el
mismo silencio que "nadie pasa parametros de mas" — que es la confusion que esta
instrumentacion existe para deshacer.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.surfaces import unknown_args


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    unknown_args.reset()
    monkeypatch.delenv("MEMORYMASTER_UNKNOWN_ARGS_LOG", raising=False)
    monkeypatch.delenv("MEMORYMASTER_UNKNOWN_ARGS_DISABLED", raising=False)
    yield
    unknown_args.reset()


# --- lo que no puede pasar -------------------------------------------------

def test_recording_never_breaks_the_call():
    """Entradas basura no levantan: esto corre en TODA llamada MCP.

    El contrato es NO LEVANTAR, no devolver vacio — con objetos sueltos el
    registro los reporta como desconocidos y esta bien. Lo unico inaceptable
    seria que una medicion tumbe la herramienta que esta midiendo.
    """
    for provistos, declarados in (
        (None, None),
        ("no soy lista", 5),
        ([object(), object()], [object()]),
        ([1, 2, 3], None),
    ):
        resultado = unknown_args.record_unknown_arguments("t", provistos or [], declarados or [])
        assert isinstance(resultado, list)


def test_a_broken_log_destination_does_not_break_the_call(tmp_path: Path, monkeypatch):
    """Un destino imposible degrada a solo-memoria, no a excepcion."""
    imposible = tmp_path / "archivo.txt"
    imposible.write_text("soy un archivo, no un directorio", encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_UNKNOWN_ARGS_LOG", str(imposible / "adentro" / "log.jsonl"))

    assert unknown_args.record_unknown_arguments("t", ["pepe"], ["real"]) == ["pepe"]
    assert unknown_args.snapshot()["total_unknown_calls"] == 1, (
        "el contador en memoria se perdio cuando fallo el disco; la evidencia "
        "no puede depender de que el archivo se pueda escribir"
    )


# --- que efectivamente mida ------------------------------------------------

def test_an_unknown_argument_is_recorded():
    assert unknown_args.record_unknown_arguments("list_claims", ["ids", "scope"], ["ids"]) == ["scope"]
    filas = unknown_args.snapshot()["rows"]
    assert filas == [{"tool": "list_claims", "argument": "scope", "count": 1}]


def test_known_arguments_record_nothing():
    """Contra-caso: si contara los conocidos, el reporte seria ruido puro."""
    assert unknown_args.record_unknown_arguments("t", ["a", "b"], ["a", "b", "c"]) == []
    assert unknown_args.snapshot()["total_unknown_calls"] == 0


def test_repeats_accumulate():
    for _ in range(3):
        unknown_args.record_unknown_arguments("query_memory", ["scope"], ["scope_allowlist"])
    assert unknown_args.snapshot()["rows"][0]["count"] == 3


def test_the_durable_log_survives_the_process(tmp_path: Path, monkeypatch):
    """En memoria no alcanza: reiniciar el server no puede borrar la evidencia."""
    log = tmp_path / "sub" / "unknown.jsonl"
    monkeypatch.setenv("MEMORYMASTER_UNKNOWN_ARGS_LOG", str(log))

    unknown_args.record_unknown_arguments("forget", ["dry_run"], ["apply"])
    unknown_args.reset()  # simula el reinicio

    assert log.exists()
    registro = json.loads(log.read_text(encoding="utf-8").strip())
    assert registro["tool"] == "forget"
    assert registro["unknown"] == ["dry_run"]
    assert unknown_args.snapshot()["total_unknown_calls"] == 0


def test_it_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_UNKNOWN_ARGS_DISABLED", "1")
    assert unknown_args.record_unknown_arguments("t", ["pepe"], ["real"]) == []
    assert unknown_args.snapshot()["disabled"] is True


# --- de punta a punta, por la superficie real ------------------------------

@pytest.mark.asyncio
async def test_the_mcp_surface_records_what_it_silently_drops(tmp_path: Path, monkeypatch):
    """El caso real: `scope` no existe en list_claims y se evapora.

    Antes de esto no quedaba ni rastro de que alguien lo hubiera intentado.
    """
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    monkeypatch.setenv("MEMORYMASTER_UNKNOWN_ARGS_LOG", str(tmp_path / "log.jsonl"))
    from memorymaster.surfaces import mcp_server as m

    try:
        await m.mcp.call_tool(
            "list_claims",
            {"db": str(tmp_path / "x.db"), "workspace": str(tmp_path), "scope": "inventado"},
        )
    except Exception:  # noqa: BLE001 - la llamada puede fallar por otras razones
        pass

    filas = unknown_args.snapshot()["rows"]
    assert any(f["tool"] == "list_claims" and f["argument"] == "scope" for f in filas), (
        f"la superficie descarto 'scope' sin registrarlo; filas={filas}"
    )
