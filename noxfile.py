"""Test sessions for MemoryMaster (T-0764).

`unit` and `ml` run in separate processes and separate uv-built virtualenvs, so
the torch / sentence-transformers / Qdrant tests never share an interpreter with
the rest of the suite. Mixed in one Windows process they segfault (exit 139) or
hang inside real-model loads; see pytest.ini.

    python -m nox                  # unit, then ml
    python -m nox -s unit -- -x    # extra pytest arguments after --
    python -m nox -s ml
"""
from __future__ import annotations

import os

import nox

nox.options.default_venv_backend = "uv"
nox.options.sessions = ["unit", "ml"]
nox.options.reuse_venv = "yes"

PYTHON = "3.12"
# Same extras as the CI test matrix (.github/workflows/ci.yml).
UNIT_EXTRAS = "dev,mcp,security,postgres"
# The ml session also installs the model stack, so its tests run instead of skipping.
ML_EXTRAS = UNIT_EXTRAS + ",embeddings,vector,graph,ml"
PYTEST_ARGS = ("-q", "--tb=short", "-p", "no:cacheprovider")
# The operator's shell carries the live configuration (dreaming providers, Jev mode,
# provider keys). Inherited by the tests it failed 51 of them on 2026-10-01 and let
# some reach real provider endpoints; with it removed the same tests pass.
LIVE_CONFIG_PREFIXES = ("MEMORYMASTER_",)
LIVE_CONFIG_NAMES = ("TYPESAFE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE", "ANTHROPIC_API_KEY")


def _without_live_config() -> None:
    """Drop the live configuration from what the sessions' commands inherit."""
    for name in list(os.environ):
        if name.startswith(LIVE_CONFIG_PREFIXES) or name in LIVE_CONFIG_NAMES:
            del os.environ[name]


@nox.session(python=PYTHON)
def unit(session: nox.Session) -> None:
    """Everything except the `ml` marker, without the model stack installed."""
    _without_live_config()
    session.install("-e", f".[{UNIT_EXTRAS}]")
    session.run("pytest", "tests", "-m", "not ml", *PYTEST_ARGS, *session.posargs)


@nox.session(python=PYTHON)
def ml(session: nox.Session) -> None:
    """Only the `ml` marker: torch, sentence-transformers, Qdrant and Kuzu paths."""
    _without_live_config()
    session.install("-e", f".[{ML_EXTRAS}]")
    session.run("pytest", "tests", "-m", "ml", *PYTEST_ARGS, *session.posargs)
