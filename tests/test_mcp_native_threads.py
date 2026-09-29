"""The stdio MCP server caps native BLAS/OpenMP threads before any ML import (T-0725).

With the default 32 threads each server reserved ~1.9 GB of private commit once
sentence-transformers/torch loaded; twenty sessions held 36.9 GB. One thread per
library brings a fresh server under 600 MB. An operator's explicit value wins.
"""
from types import SimpleNamespace

import pytest

from memorymaster.recall import embeddings
from memorymaster.surfaces import mcp_server

THREAD_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")


def _start(monkeypatch, env, os_name, load):
    monkeypatch.setattr(mcp_server, "_environ", env, raising=False)
    monkeypatch.delenv("MEMORYMASTER_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setattr(embeddings.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(mcp_server, "os", SimpleNamespace(name=os_name))
    monkeypatch.setattr("importlib.import_module", load)
    monkeypatch.setattr(mcp_server, "mcp", SimpleNamespace(run=lambda: None))
    monkeypatch.setattr(mcp_server, "FastMCP", object())
    assert mcp_server.main() == 0


def test_threads_are_capped_before_the_windows_ml_import(monkeypatch):
    env: dict[str, str] = {}
    seen = []
    _start(monkeypatch, env, "nt", lambda name: seen.append({var: env.get(var) for var in THREAD_VARS}))
    assert seen == [{var: "1" for var in THREAD_VARS}]


def test_threads_are_capped_when_ml_loads_lazily_later(monkeypatch):
    env: dict[str, str] = {}
    _start(monkeypatch, env, "posix", lambda name: pytest.fail("unexpected ML import"))
    assert {var: env.get(var) for var in THREAD_VARS} == {var: "1" for var in THREAD_VARS}


def test_operator_thread_setting_wins(monkeypatch):
    env = {"OMP_NUM_THREADS": "4"}
    _start(monkeypatch, env, "posix", lambda name: None)
    assert env == {"OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
