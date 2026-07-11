"""Red contracts for authoritative filtering of Qdrant candidates (MM-SEC-02)."""
from __future__ import annotations

from collections.abc import Iterable

import pytest

import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


AUDIT_BASELINE = pytest.mark.xfail(
    strict=True,
    reason="audit baseline MM-SEC-02: Qdrant bypasses authoritative policy filtering",
)


class FakeQdrant:
    """Network-free vector candidate source controlled by each test."""

    def __init__(self, hits: Iterable[dict]) -> None:
        self._hits = list(hits)
        self.closed = False

    def search(self, query_text: str, limit: int = 5) -> list[dict]:
        del query_text
        return self._hits[:limit]

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def isolated_mcp_state(monkeypatch: pytest.MonkeyPatch):
    mcp_server._INGEST_RATE_BUCKETS.clear()
    monkeypatch.setenv("MM_INGEST_RATE_LIMIT_PER_MIN", "0")
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_PROJECT_SCOPE", "")
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_WORKSPACE", "")
    monkeypatch.setattr(mcp_server, "_ENV_QUERY_INCLUDE_LEGACY_PROJECT", False)
    yield
    mcp_server._INGEST_RATE_BUCKETS.clear()


def _install_fake_qdrant(monkeypatch: pytest.MonkeyPatch, hits: list[dict]) -> FakeQdrant:
    from memorymaster.recall import qdrant_backend

    fake = FakeQdrant(hits)
    monkeypatch.setattr(qdrant_backend, "QdrantBackend", lambda: fake)
    return fake


def _init_db(tmp_path) -> tuple[str, str, MemoryService]:
    workspace_path = tmp_path / "allowed"
    workspace_path.mkdir()
    db = str(tmp_path / "qdrant-policy.db")
    svc = MemoryService(db, workspace_root=workspace_path)
    svc.init_db()
    return db, str(workspace_path), svc


@AUDIT_BASELINE
def test_qdrant_orphan_payload_is_never_returned(tmp_path, monkeypatch) -> None:
    """A vector point is only an ID candidate; payload is never authoritative."""
    db, workspace, _svc = _init_db(tmp_path)
    fake = _install_fake_qdrant(
        monkeypatch,
        [
            {
                "claim_id": 999_999,
                "score": 0.99,
                "payload": {
                    "claim_id": 999_999,
                    "text": "orphan payload must not escape",
                    "state": "confirmed",
                    "scope": "project:allowed",
                },
            }
        ],
    )

    result = mcp_server.query_memory(
        query="orphan payload",
        db=db,
        workspace=workspace,
        retrieval_mode="qdrant",
        scope_allowlist="project:allowed",
        include_candidates=False,
        include_stale=False,
        include_conflicted=False,
    )

    assert result["rows"] == 0
    assert result["claims"] == []
    assert fake.closed is True


@AUDIT_BASELINE
def test_qdrant_filters_archived_and_wrong_scope_rows(tmp_path, monkeypatch) -> None:
    """Primary-store lifecycle and scope policy must filter every vector hit."""
    db, workspace, svc = _init_db(tmp_path)
    archived = svc.ingest(
        "archived vector policy marker",
        [CitationInput(source="test://archived")],
        scope="project:allowed",
    )
    transition_claim(
        svc.store,
        archived.id,
        "archived",
        reason="adversarial fixture",
        event_type="transition",
    )
    foreign = svc.ingest(
        "foreign vector policy marker",
        [CitationInput(source="test://foreign")],
        scope="project:other",
    )
    _install_fake_qdrant(
        monkeypatch,
        [
            {"claim_id": archived.id, "score": 0.98, "payload": {"state": "confirmed"}},
            {"claim_id": foreign.id, "score": 0.97, "payload": {"state": "confirmed"}},
        ],
    )

    result = mcp_server.query_memory(
        query="vector policy marker",
        db=db,
        workspace=workspace,
        retrieval_mode="qdrant",
        scope_allowlist="project:allowed",
        include_candidates=False,
        include_stale=False,
        include_conflicted=False,
    )

    assert result["rows"] == 0
    assert result["claims"] == []
