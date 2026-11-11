"""El log de la vault no materializa un vault donde no lo hay.

QUE PASO, MEDIDO EL 2026-08-26. `_get_log_path` caia a
`Path("obsidian-vault")/"log.md"` — RELATIVO AL CWD — cuando no recibia destino
ni MEMORYMASTER_VAULT_DIR. Resultado: 40 carpetas de proyecto bajo Py Apps con
un obsidian-vault/ que nadie pidio, 1,4 MB, y 8 de ellas con el archivo
COMMITEADO a su repo (bajoneando, brlite, damore2, interonda, mutual, pather,
venezia, yolo26), donde borrarlo del disco no lo saca del historial.

EL DISPARADOR NO ERA `ingest_claim`, y esa correccion importa. El camino MCP ya
estaba guardado con `if _wiki_absorb_enabled()`. Un pane par aporto el
contraejemplo que lo probo: ~25 ingests desde su cwd sin tocar su log.md. El
disparador real es `lint-vault` — un comando de DIAGNOSTICO, no una operacion de
wiki — reproducido creando el archivo desde un cwd limpio.

POR QUE EL ARREGLO VA EN _get_log_path Y NO EN EL LLAMADOR: guardar un llamador
no alcanza cuando el destino equivocado lo elige la funcion compartida. Las ocho
funciones log_* pasan por append_log -> _get_log_path. Guardar solo lint-vault
habria arreglado el sitio 1 de 8.

`test_a_configured_vault_still_receives_the_entry` es el control positivo: sin
el, una funcion que nunca escribe pasaria todos los casos de abstencion y el
archivo entero seria decorativo.
"""
from __future__ import annotations

import pathlib

import pytest

from memorymaster.knowledge import vault_log


@pytest.fixture(autouse=True)
def _sin_vault_configurado(monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_VAULT_DIR", raising=False)
    yield


# --- lo que no puede volver a pasar ---------------------------------------

def test_no_vault_is_created_in_the_working_directory(tmp_path, monkeypatch):
    """El caso exacto que ensucio 40 proyectos."""
    monkeypatch.chdir(tmp_path)

    vault_log.log_lint({"contradictions": [], "orphans": [], "gaps": [], "stale": []})

    assert not (tmp_path / "obsidian-vault").exists(), (
        "lint materializo un vault relativo al cwd; es el bug que dejo 40 "
        "carpetas de proyecto con un obsidian-vault/ que nadie pidio"
    )


@pytest.mark.parametrize(
    "operacion",
    ["log_ingest", "log_query", "log_lint", "log_curate", "log_steward", "log_sync"],
)
def test_every_log_helper_abstains_without_a_vault(operacion, tmp_path, monkeypatch):
    """Las ocho pasan por el mismo cuello; se comprueban como grupo.

    Guardar una sola dejaria vivas las otras siete, que es exactamente el modo
    de falla que este arreglo evita.
    """
    monkeypatch.chdir(tmp_path)
    argumentos = {
        "log_ingest": (1, "sujeto", "project:x"),
        "log_query": ("consulta", 0),
        "log_lint": ({},),
        "log_curate": ({},),
        "log_steward": ({},),
        "log_sync": ("push", 0),
    }[operacion]

    getattr(vault_log, operacion)(*argumentos)

    assert list(tmp_path.iterdir()) == [], (
        f"{operacion} escribio algo sin vault configurado: "
        f"{[p.name for p in tmp_path.iterdir()]}"
    )


def test_the_path_resolver_reports_no_destination(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert vault_log._get_log_path() is None
    assert vault_log._get_log_path(None) is None


# --- control positivo: con destino, SI escribe ----------------------------

def test_a_configured_vault_still_receives_the_entry(tmp_path):
    """Sin este caso, una funcion que nunca escribe pasaria todo lo de arriba."""
    destino = tmp_path / "mi-vault"

    vault_log.log_lint({"contradictions": [1], "orphans": [], "gaps": [], "stale": []}, destino)

    log = destino / "log.md"
    assert log.exists(), "no escribio pese a recibir un destino explicito"
    assert "lint" in log.read_text(encoding="utf-8")


def test_the_environment_variable_still_works(tmp_path, monkeypatch):
    destino = tmp_path / "vault-por-env"
    monkeypatch.setenv("MEMORYMASTER_VAULT_DIR", str(destino))

    vault_log.log_ingest(7, "sujeto", "project:x")

    assert (destino / "log.md").exists()
    assert "claim #7" in (destino / "log.md").read_text(encoding="utf-8")


def test_an_explicit_destination_wins_over_the_environment(tmp_path, monkeypatch):
    """Precedencia: el argumento manda. Al reves, un env viejo secuestraria la escritura."""
    por_env = tmp_path / "env"
    explicito = tmp_path / "explicito"
    monkeypatch.setenv("MEMORYMASTER_VAULT_DIR", str(por_env))

    vault_log.log_curate({"claims": 1, "topics": 1, "files_written": 1}, explicito)

    assert (explicito / "log.md").exists()
    assert not por_env.exists()
