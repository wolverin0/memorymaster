"""La version esperada se mueve sola con la release, no a mano.

QUE PASABA. `expected_version` vivia en un config FUERA del repo (AppData) que
habia que actualizar en cada release. Fallo dos veces: 31 corridas seguidas en
FAIL con 4.7.6 contra 4.8.4 (2026-08-20), y otra vez el 2026-08-29 con 4.8.4
contra 4.8.5, apenas se lanzo la version. Bumpear y actualizar el config eran dos
actos unidos solo por la memoria de alguien, y ningun test del repo podia
vigilarlo porque ese archivo no existe en CI.

Leer el pyproject del workspace mueve la referencia sola. Y en una instalacion
editable detecta la falla que importa: bumpeaste y no reinstalaste, o sea que el
paquete que corre NO es el codigo que commiteaste.

Las dos ramas, y el caso "sin referencia" que no debe inventar una expectativa.
"""
from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest

from memorymaster.operations.operational_review import (
    ReviewConfig,
    Verdict,
    check_runtime,
)

INSTALADA = importlib.metadata.version("memorymaster")


def _workspace(tmp_path: Path, version: str | None) -> Path:
    """Devuelve una ruta de base dentro de un checkout falso."""
    if version is not None:
        (tmp_path / "pyproject.toml").write_text(
            f'[project]\nname = "memorymaster"\nversion = "{version}"\n', encoding="utf-8"
        )
    db = tmp_path / "memorymaster.db"
    db.write_bytes(b"")
    return db


def test_matching_pyproject_passes_without_any_config(tmp_path):
    db = _workspace(tmp_path, INSTALADA)
    r = check_runtime(ReviewConfig(db=db))
    assert r.verdict is Verdict.PASS
    assert "pyproject" in r.detail, "el veredicto tiene que DECIR de donde saco la referencia"


def test_a_bumped_pyproject_without_reinstall_fails(tmp_path):
    """El caso real del 2026-08-29: se bumpeo a 4.8.5 y el egg-info seguia en 4.8.4."""
    db = _workspace(tmp_path, "99.99.99")
    r = check_runtime(ReviewConfig(db=db))
    assert r.verdict is Verdict.FAIL
    assert INSTALADA in r.detail and "99.99.99" in r.detail


def test_an_explicit_config_still_wins(tmp_path):
    """Quien lo fije a proposito manda; no le rompemos el caso de uso."""
    db = _workspace(tmp_path, INSTALADA)
    r = check_runtime(ReviewConfig(db=db, expected_version="1.2.3"))
    assert r.verdict is Verdict.FAIL
    assert "(config)" in r.detail


def test_no_reference_anywhere_does_not_invent_one(tmp_path):
    """Sin pyproject ni config no hay nada que comparar.

    Inventar una expectativa aca convertiria una instalacion desde wheel en un
    FAIL permanente, que es el mismo defecto que este cambio viene a sacar.

    HERMETICO A PROPOSITO: `_workspace_version` camina TODOS los padres del path
    de la base, asi que si tmp_path colgara de un arbol con pyproject.toml este
    test medira otra cosa sin avisar. Se verifica y se saltea en vez de dar un
    verde que no significa nada — hoy mismo una corrida no hermetica me hizo
    declarar un arreglo que CI despues rechazo.
    """
    ancestro = next(
        (d for d in [tmp_path, *tmp_path.parents] if (d / "pyproject.toml").exists()), None
    )
    if ancestro is not None:
        pytest.skip(f"tmp_path cuelga de un checkout con pyproject: {ancestro}")
    db = _workspace(tmp_path, None)
    r = check_runtime(ReviewConfig(db=db))
    assert r.verdict is Verdict.PASS
    assert "sin referencia" in r.detail
