"""F-17: import sentence-transformers before stdio only when it will be used.

Review repros ``review-A/r6_st_import.py`` / ``r6b_startup.py``: the Windows
stdio stall fix (``f8146e6``) imported sentence-transformers/torch
unconditionally before ``mcp.run()`` -- 6-18 s before the server answers --
even when the effective embedding provider never loads it.  The eager import
must stay for the provider that does need it (the stall fix), and be skipped
when the configured provider is ``hash`` or ``gemini`` or the package is
absent.  The provider factory must honour the same configuration, otherwise a
skipped eager import would come back later as the post-reader stall.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from memorymaster.recall import embeddings
from memorymaster.surfaces import mcp_server


def _run_main(monkeypatch):
    loaded: list[str] = []
    monkeypatch.setattr(mcp_server, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr("importlib.import_module", lambda name: loaded.append(name))
    monkeypatch.setattr(mcp_server, "mcp", SimpleNamespace(run=lambda: None))
    monkeypatch.setattr(mcp_server, "FastMCP", object())
    assert mcp_server.main() == 0
    return loaded


@pytest.mark.parametrize("provider", ["hash", "gemini"])
def test_windows_stdio_skips_ml_import_when_provider_does_not_use_it(monkeypatch, provider):
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", provider)
    assert _run_main(monkeypatch) == []


def test_windows_stdio_skips_ml_import_when_package_is_absent(monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setattr(embeddings.importlib.util, "find_spec", lambda name: None)
    assert _run_main(monkeypatch) == []


@pytest.mark.parametrize("provider", [None, "auto", "sentence-transformers"])
def test_windows_stdio_keeps_the_stall_fix_when_provider_needs_it(monkeypatch, provider):
    if provider is None:
        monkeypatch.delenv("MEMORYMASTER_EMBEDDING_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", provider)
    monkeypatch.setattr(embeddings.importlib.util, "find_spec", lambda name: object())
    assert _run_main(monkeypatch) == ["sentence_transformers"]


def test_hash_provider_config_never_loads_sentence_transformers(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setattr(
        embeddings, "create_semantic_provider",
        lambda *a, **k: pytest.fail("hash configuration loaded sentence-transformers"),
    )
    provider = embeddings.create_best_provider()
    assert provider.model == "hash-v1"


def test_gemini_provider_config_falls_back_to_hash_not_sentence_transformers(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(
        embeddings, "create_semantic_provider",
        lambda *a, **k: pytest.fail("gemini configuration loaded sentence-transformers"),
    )
    assert embeddings.create_best_provider().model == "hash-v1"


def test_unknown_provider_value_keeps_auto_behaviour(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", "nonsense")
    assert embeddings.configured_embedding_provider() == "auto"


@pytest.mark.parametrize("provider", ["hash", "gemini"])
def test_local_rerank_keeps_the_stall_fix_even_with_a_non_st_provider(monkeypatch, provider):
    """Verifier note: the local cross-encoder rerank also loads
    sentence-transformers lazily after stdio starts, so ``hash`` + rerank
    must keep the pre-import or the Windows stall comes back."""
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", provider)
    monkeypatch.setenv("MEMORYMASTER_RECALL_RERANK_LOCAL", "1")
    monkeypatch.setattr(embeddings.importlib.util, "find_spec", lambda name: object())
    assert _run_main(monkeypatch) == ["sentence_transformers"]


def test_local_rerank_off_by_default_does_not_force_the_import(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", "hash")
    monkeypatch.delenv("MEMORYMASTER_RECALL_RERANK_LOCAL", raising=False)
    monkeypatch.setattr(embeddings.importlib.util, "find_spec", lambda name: object())
    assert _run_main(monkeypatch) == []
