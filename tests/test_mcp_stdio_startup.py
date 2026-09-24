"""Keep optional native ML imports ahead of Windows stdio reader startup."""
from types import SimpleNamespace

import pytest

from memorymaster.recall import embeddings
from memorymaster.surfaces import mcp_server


@pytest.mark.parametrize("failure", [None, ImportError, OSError, RuntimeError])
def test_windows_stdio_initializes_optional_native_imports_before_reader(monkeypatch, failure):
    loaded = []

    def load(name):
        loaded.append(name)
        if failure:
            raise failure("optional package unavailable")

    def start_reader():
        # A tool may need native ML modules immediately after the reader starts.
        assert loaded == ["sentence_transformers"]

    # The effective provider needs sentence-transformers (auto + installed);
    # the skip cases live in test_mcp_stdio_embedding_provider.py (F-17).
    monkeypatch.delenv("MEMORYMASTER_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setattr(embeddings.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(mcp_server, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr("importlib.import_module", load)
    monkeypatch.setattr(mcp_server, "mcp", SimpleNamespace(run=start_reader))
    monkeypatch.setattr(mcp_server, "FastMCP", object())
    assert mcp_server.main() == 0


def test_non_windows_stdio_keeps_optional_ml_imports_lazy(monkeypatch):
    monkeypatch.setattr(mcp_server, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr("importlib.import_module", lambda name: pytest.fail("unexpected ML import"))
    monkeypatch.setattr(mcp_server, "mcp", SimpleNamespace(run=lambda: None))
    monkeypatch.setattr(mcp_server, "FastMCP", object())
    assert mcp_server.main() == 0


def test_missing_mcp_fails_before_optional_ml_import(monkeypatch):
    monkeypatch.setattr(mcp_server, "FastMCP", None)
    monkeypatch.setattr("importlib.import_module", lambda name: pytest.fail("unexpected ML import"))
    with pytest.raises(RuntimeError, match="MCP support is not installed"):
        mcp_server.main()
