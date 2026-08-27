"""run-dashboard: nunca abrir (ni crear) una base fantasma sin decirlo.

QUE PASO (2026-08-26). El dashboard se lanzo desde el home del operador con
`--db memorymaster.db` relativo. La ruta resolvio contra ESE cwd, donde vivia
una base fantasma de 31 claims de abril, y el dashboard la mostro sin ninguna
indicacion de que base era: "no such table" para tablas que la base real si
tiene, y una cola con ids #12-17 en un corpus que va por 135.000. El operador
lo describio exacto: "esta lleno de errores". No estaba roto — miraba otra base.

Es el mismo genero de bug que el vault huerfano de lint-vault (commit d284cb2):
un recurso relativo al cwd, materializado o abierto donde nadie lo pidio.

LAS DOS DEFENSAS QUE ESTE ARCHIVO CLAVA:
1. Una base inexistente se RECHAZA (exit 2), no se crea vacia — y el test
   verifica ademas que el archivo NO aparecio, porque un dashboard que crea la
   base que va a mostrar es un generador de fantasmas.
2. La ruta se resuelve a ABSOLUTA antes de abrir, asi el banner nombra el
   artefacto real — la regla de la semana: un verde solo vale si nombra lo que
   produjo.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from memorymaster.surfaces.cli_handlers_basic import _handle_run_dashboard


def _args(**extra) -> argparse.Namespace:
    base = {
        "workspace": ".",
        "host": "127.0.0.1",
        "port": 0,
        "operator_log_jsonl": "artifacts/operator/operator_events.jsonl",
    }
    base.update(extra)
    return argparse.Namespace(**base)


class _ServerStub:
    def __init__(self) -> None:
        self.closed = False

    def serve_forever(self) -> None:  # devuelve al toque: el test no sirve trafico
        return None

    def server_close(self) -> None:
        self.closed = True


# --- defensa 1: la base inexistente se rechaza, no se crea -----------------

def test_a_missing_db_is_refused_with_exit_2(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    rc = _handle_run_dashboard(_args(), None, None, "memorymaster.db")

    assert rc == 2
    salida = capsys.readouterr().out
    assert "no existe" in salida
    assert str(tmp_path) in salida, (
        "el error debe nombrar la ruta ABSOLUTA que intento abrir — sin eso el "
        "operador no puede ver que el cwd era el equivocado"
    )


def test_refusing_does_not_materialize_the_phantom(tmp_path: Path, monkeypatch):
    """La defensa central: rechazar sin crear. Un dashboard que crea la base
    que va a mostrar fabrica el fantasma que este bug puso en el home."""
    monkeypatch.chdir(tmp_path)

    _handle_run_dashboard(_args(), None, None, "memorymaster.db")

    assert not (tmp_path / "memorymaster.db").exists(), (
        "el rechazo creo la base fantasma que debia impedir"
    )


# --- defensa 2: con base real, se abre la ABSOLUTA y el banner la nombra ---

def test_an_existing_db_is_opened_by_absolute_path(tmp_path: Path, monkeypatch, capsys):
    import sqlite3

    db = tmp_path / "memorymaster.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO claims (id) VALUES (1), (2), (3)")
    conn.commit(); conn.close()
    monkeypatch.chdir(tmp_path)

    capturado: dict = {}

    def _fake_server(**kwargs):
        capturado.update(kwargs)
        return _ServerStub()

    monkeypatch.setattr(
        "memorymaster.surfaces.dashboard.create_dashboard_server", _fake_server,
    )

    rc = _handle_run_dashboard(_args(), None, None, "memorymaster.db")

    assert rc == 0
    assert Path(capturado["db_target"]).is_absolute(), (
        f"el server recibio una ruta relativa: {capturado['db_target']}"
    )
    salida = capsys.readouterr().out
    assert str(db.resolve()) in salida, "el banner no nombra la base que abrio"
    assert "3 claims" in salida, (
        "el banner debe decir cuantas claims tiene la base — 31 contra 135.000 "
        "es la diferencia entre detectar el fantasma al instante o no"
    )


def test_a_dsn_target_is_passed_through_untouched(monkeypatch):
    """postgres:// no es una ruta: ni resolver ni exigir que exista."""
    capturado: dict = {}

    def _fake_server(**kwargs):
        capturado.update(kwargs)
        return _ServerStub()

    monkeypatch.setattr(
        "memorymaster.surfaces.dashboard.create_dashboard_server", _fake_server,
    )

    rc = _handle_run_dashboard(
        _args(), None, None, "postgresql://user@host/dbname",
    )

    assert rc == 0
    assert capturado["db_target"] == "postgresql://user@host/dbname"
