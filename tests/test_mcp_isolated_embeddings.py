"""T-2004: a long-lived MCP server never maps torch; MiniLM runs in a disposable child.

A shared server that imported sentence-transformers held ~2.65 GB of private memory
for a model it only needs on a hybrid query. The model now lives in a child process
(`dense_worker --model`), started by the first embedding and ended when idle.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from memorymaster.recall import dense_worker, embeddings

REPO = Path(__file__).parents[1]

# A child that speaks the worker protocol without torch: vector = [len(text), 1, 0].
FAKE_CHILD = textwrap.dedent(
    """
    import numpy as np
    from memorymaster.recall import dense_worker
    dense_worker.run(lambda: (lambda texts, kind: np.array([[len(t), 1.0, 0.0] for t in texts], dtype=np.float32), "cpu"))
    """
)


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed interpreter and inline code
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=REPO, capture_output=True, text=True, timeout=120,
        env={**os.environ, "MEMORYMASTER_MCP_HTTP_TOKEN": "test-token-not-a-secret"},
    )


def _isolate(monkeypatch, child: list[str]) -> None:
    monkeypatch.setattr(embeddings, "_ISOLATE", True)
    monkeypatch.setattr(embeddings, "_TRANSFORMERS", {})
    monkeypatch.setattr(embeddings.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(dense_worker, "child_command", lambda *args: child)


def test_building_the_http_server_does_not_import_torch(tmp_path):
    result = _run(
        f"""
        import sys
        from memorymaster.surfaces import mcp_http, mcp_server
        mcp_http._preload_native_ml()
        mcp_http.create_http_app(db_target=r"{tmp_path / 'm.db'}", workspace=r"{tmp_path}")
        print("torch" in sys.modules, "sentence_transformers" in sys.modules)
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "False"]


def test_a_hybrid_embedding_goes_through_the_child_not_this_process(monkeypatch):
    _isolate(monkeypatch, [sys.executable, "-c", FAKE_CHILD])
    monkeypatch.delenv("MEMORYMASTER_EMBEDDING_PROVIDER", raising=False)
    provider = embeddings.create_best_provider()
    try:
        assert provider.model == "all-MiniLM-L6-v2" and provider.dims == 3 and provider.is_semantic
        assert provider.embed("abcd") == [4.0, 1.0, 0.0]  # a single text -> one plain vector
        assert provider.embed_batch(["a", "abc"]) == [[1.0, 1.0, 0.0], [3.0, 1.0, 0.0]]
        assert len(embeddings._TRANSFORMERS) == 1  # one child for the whole process
    finally:
        for transformer in embeddings._TRANSFORMERS.values():
            transformer.close()


def test_a_dead_child_falls_back_like_a_missing_model(monkeypatch):
    _isolate(monkeypatch, [sys.executable, "-c", "raise SystemExit(3)"])
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", "sentence-transformers")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert embeddings.create_best_provider().model == "hash-v1"


def test_without_the_package_the_isolated_path_raises_import_error(monkeypatch):
    monkeypatch.setattr(embeddings, "_ISOLATE", True)
    monkeypatch.setattr(embeddings, "_TRANSFORMERS", {})
    monkeypatch.setattr(embeddings.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ImportError):
        embeddings._load_transformer("all-MiniLM-L6-v2")
