from __future__ import annotations

import pytest

from memorymaster.recall import verbatim_store
from memorymaster.recall import qdrant_recall_fallback
from memorymaster.recall.qdrant_backend import QdrantBackend
from memorymaster.surfaces import mcp_server


@pytest.mark.parametrize("requested_mode", ["vector", "hybrid"])
def test_public_verbatim_search_downgrades_qdrant_modes_to_fts(
    monkeypatch: pytest.MonkeyPatch,
    requested_mode: str,
) -> None:
    authoritative = [
        {
            "id": 7,
            "content": "authoritative SQLite row",
            "scope": "project:test",
            "score": 1.0,
            "source": "fts",
        }
    ]
    monkeypatch.setattr(
        verbatim_store,
        "_search_fts",
        lambda db_path, query, scope, limit: authoritative,
    )
    monkeypatch.setattr(
        verbatim_store,
        "_search_vector",
        lambda *args, **kwargs: pytest.fail("quarantined Qdrant search was invoked"),
    )

    rows = verbatim_store.search_verbatim(
        "unused.db",
        "policy boundary",
        scope="project:test",
        mode=requested_mode,
    )

    assert rows == authoritative


def test_public_verbatim_search_never_returns_raw_qdrant_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verbatim_store, "_search_fts", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        verbatim_store,
        "_search_vector",
        lambda *args, **kwargs: [
            {
                "id": "orphan-point",
                "content": "raw payload must not escape",
                "scope": "project:other-tenant",
                "source": "vector",
                "score": 1.0,
            }
        ],
    )

    rows = verbatim_store.search_verbatim(
        "unused.db",
        "raw payload",
        scope="project:test",
        mode="vector",
    )

    assert rows == []


def test_internal_verbatim_qdrant_adapter_fails_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verbatim_store, "QDRANT_URL", "http://qdrant.invalid")
    monkeypatch.setattr(
        verbatim_store.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("quarantined adapter reached the network"),
    )

    with pytest.raises(PermissionError, match="quarantined"):
        verbatim_store._search_vector("raw payload", "project:test", 5)


def test_claim_qdrant_backend_search_fails_before_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = QdrantBackend(
        qdrant_url="http://qdrant.invalid",
        ollama_url="http://ollama.invalid",
    )
    monkeypatch.setattr(
        backend,
        "_embed",
        lambda *args, **kwargs: pytest.fail("quarantined adapter embedded the query"),
    )

    with pytest.raises(PermissionError, match="quarantined"):
        backend.search("raw payload")

    backend.close()


def test_recall_fallback_search_fails_before_loading_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        qdrant_recall_fallback,
        "_get_embedder",
        lambda: pytest.fail("quarantined adapter loaded the embedding model"),
    )

    with pytest.raises(PermissionError, match="quarantined"):
        qdrant_recall_fallback.search("raw payload")


@pytest.mark.parametrize("requested_mode", ["vector", "hybrid"])
def test_mcp_verbatim_search_reports_qdrant_containment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    requested_mode: str,
) -> None:
    observed: dict[str, str] = {}

    def fake_search(db_path, query, scope, limit, mode):
        observed["mode"] = mode
        return []

    monkeypatch.setattr(verbatim_store, "search_verbatim", fake_search)

    result = mcp_server.search_verbatim(
        query="policy boundary",
        db=str(tmp_path / "verbatim.db"),
        scope="project:test",
        mode=requested_mode,
    )

    assert observed["mode"] == "fts"
    assert result["requested_mode"] == requested_mode
    assert result["mode"] == "fts"
    assert result["containment_reason"]
