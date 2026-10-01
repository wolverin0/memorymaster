"""noxfile.py keeps the ML tests out of the unit interpreter (T-0764).

Mixed in one Windows process the torch/sentence-transformers tests segfault or
hang; the sessions exist to make that separation the default way to run tests.
"""
from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

nox = pytest.importorskip("nox")


@lru_cache(maxsize=1)  # nox registers sessions globally; load the file once
def _noxfile():
    spec = importlib.util.spec_from_file_location("memorymaster_noxfile", ROOT / "noxfile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Session:
    def __init__(self, posargs=()):
        self.posargs = list(posargs)
        self.installs: list[tuple[str, ...]] = []
        self.runs: list[tuple[str, ...]] = []

    def install(self, *args):
        self.installs.append(args)

    def run(self, *args):
        self.runs.append(args)


def _call(session_func, session):
    getattr(session_func, "func", session_func)(session)


def test_uv_backend_and_both_sessions_by_default():
    module = _noxfile()
    assert nox.options.default_venv_backend == "uv"
    assert nox.options.sessions == ["unit", "ml"]
    assert callable(module.unit) and callable(module.ml)


def test_unit_session_excludes_ml_marker_and_model_stack():
    module, session = _noxfile(), _Session(["-x"])
    _call(module.unit, session)
    [run] = session.runs
    assert run[:4] == ("pytest", "tests", "-m", "not ml") and run[-1] == "-x"
    [install] = session.installs
    extras = install[-1].split("[", 1)[1].rstrip("]").split(",")
    assert not {"embeddings", "vector", "graph", "ml"} & set(extras)
    assert {"dev", "mcp", "security", "postgres"} <= set(extras)  # same as CI


def test_ml_session_runs_only_ml_marker_with_model_stack():
    module, session = _noxfile(), _Session()
    _call(module.ml, session)
    [run] = session.runs
    assert run[:4] == ("pytest", "tests", "-m", "ml")
    [install] = session.installs
    extras = set(install[-1].split("[", 1)[1].rstrip("]").split(","))
    assert {"embeddings", "vector", "graph", "ml"} <= extras
