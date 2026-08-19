"""El guarda del marcador tiene que estar CONECTADO, no solo existir.

Cuarta aparicion del mismo patron el 2026-08-19, y esta la escribi yo:

  1. .agent-workflow/graph.json declaraba gates que ningun codigo leia.
  2. El marcador corria contra site-packages, asi que no podia ver la rama.
  3. El arnes de LongMemEval midio bien una vez y nunca llego a main.
  4. check_probes_frozen.py se documenta a si mismo diciendo "ESTE CHECK EN CI
     frena el MERGE / no esquivable por un agente" — y no tenia job en ci.yml
     ni test. El PR que lo introdujo AFIRMABA que hacia fallar CI.

Un guarda sin caller es peor que ninguno: el texto del PR promete una barrera
que no existe, asi que nadie mas la construye. Por eso el caso 1 de este archivo
no prueba el comportamiento del script sino su CABLEADO — es el unico que habria
detectado el defecto real.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
GUARD = REPO / "scripts" / "check_probes_frozen.py"
CI = REPO / ".github" / "workflows" / "ci.yml"


def test_ci_actually_calls_the_guard():
    """El caso que fallaba. Sin esto, todo lo de abajo pasa con el guarda desconectado."""
    ci = CI.read_text(encoding="utf-8")
    assert "check_probes_frozen.py" in ci, (
        "scripts/check_probes_frozen.py no aparece en ci.yml.\n"
        "El script se documenta como la unica capa que frena el MERGE; sin un job "
        "que lo invoque no frena nada, y el PR que lo agrega dice lo contrario."
    )


def _run_git(cwd: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Un repo con el guarda, una sonda ya en base, y una rama encima."""
    r = tmp_path / "r"
    (r / "scripts" / "probes").mkdir(parents=True)
    (r / "scripts" / "check_probes_frozen.py").write_text(
        GUARD.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (r / "scripts" / "probe_suite.py").write_text("# marcador\n", encoding="utf-8")
    (r / "scripts" / "probes" / "goals.json").write_text("{}\n", encoding="utf-8")
    (r / "libre.py").write_text("# no congelado\n", encoding="utf-8")

    _run_git(r, "init", "-q", "-b", "main")
    _run_git(r, "config", "user.email", "t@t")
    _run_git(r, "config", "user.name", "t")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-qm", "base")
    _run_git(r, "checkout", "-qb", "rama")
    return r


def _guard(repo: pathlib.Path) -> subprocess.CompletedProcess:
    """Corre la copia del guarda que vive en la rama BASE, como hace CI.

    Correr la copia que trae el PR permitiria que una rama neutralice el guarda y
    que la version neutralizada certifique el cambio — el agujero que
    test_editing_the_guard_itself_fails encontro.
    """
    base_copy = repo / ".guard-from-base.py"
    shown = subprocess.run(
        ["git", "show", "main:scripts/check_probes_frozen.py"],
        cwd=repo, capture_output=True, text=True,
    )
    base_copy.write_text(
        shown.stdout if shown.returncode == 0
        else (repo / "scripts" / "check_probes_frozen.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(base_copy), "--base", "main"],
        cwd=repo, capture_output=True, text=True,
    )


def test_editing_a_frozen_probe_fails(repo: pathlib.Path):
    (repo / "scripts" / "probes" / "goals.json").write_text('{"target": 0}\n', encoding="utf-8")
    _run_git(repo, "commit", "-aqm", "aflojar la meta")

    out = _guard(repo)
    assert out.returncode == 1, "editar una meta congelada tiene que romper CI"
    assert "goals.json" in out.stderr


def test_editing_the_guard_itself_fails(repo: pathlib.Path):
    """Sin esto, el camino barato es borrarle los dientes al guarda en vez de a la sonda."""
    (repo / "scripts" / "check_probes_frozen.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
    _run_git(repo, "commit", "-aqm", "neutralizar el guarda")

    out = _guard(repo)
    assert out.returncode == 1, "el guarda tiene que congelarse a si mismo"


def test_unrelated_changes_pass(repo: pathlib.Path):
    """Contra-metrica: un guarda que bloquea todo se desactiva a la semana."""
    (repo / "libre.py").write_text("# cambio normal\n", encoding="utf-8")
    _run_git(repo, "commit", "-aqm", "trabajo comun")

    assert _guard(repo).returncode == 0


def test_first_addition_is_allowed(repo: pathlib.Path):
    """El alta inicial pasa; si no, el guarda bloquearia el PR que lo introduce."""
    (repo / "scripts" / "probes" / "cohort.json").write_text("[]\n", encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-qm", "sonda nueva")

    out = _guard(repo)
    assert out.returncode == 0
    assert "alta inicial permitida" in out.stdout
