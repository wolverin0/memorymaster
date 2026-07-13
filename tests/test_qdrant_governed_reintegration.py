"""Adversarial contracts for governed Qdrant candidate reintegration."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import qdrant_reconcile
from memorymaster.recall.planner import RetrievalRequest, build_retrieval_plan
from memorymaster.recall.qdrant_backend import (
    EMBEDDING_DIMS,
    QdrantBackend,
    QdrantCandidate,
    claim_content_hash,
)
from memorymaster.recall import qdrant_outbox


class FakeCandidates:
    def __init__(self, candidates: list[QdrantCandidate]) -> None:
        self.candidates = candidates
        self.limits: list[int] = []

    def search_candidates(self, query_text: str, *, limit: int) -> list[QdrantCandidate]:
        assert query_text
        self.limits.append(limit)
        return self.candidates[:limit]


def _seed(svc: MemoryService, text: str, *, scope: str = "project:allowed"):
    claim = svc.ingest(
        text,
        [CitationInput(source="test://qdrant-governed")],
        scope=scope,
        source_agent="seed",
    )
    transition_claim(
        svc.store,
        claim.id,
        "confirmed",
        reason="governed qdrant fixture",
        event_type="validator",
    )
    return svc.store.get_claim(claim.id, include_citations=True)


def _candidate(claim, *, content_hash: str | None = None, score: float = 0.9):
    return QdrantCandidate(
        claim_id=claim.id,
        content_hash=content_hash or claim_content_hash(claim),
        score=score,
    )


def test_qdrant_requires_explicit_candidate_read_gate() -> None:
    contained = build_retrieval_plan(
        RetrievalRequest(query_text="semantic", retrieval_mode="qdrant")
    )
    enabled = build_retrieval_plan(
        RetrievalRequest(
            query_text="semantic",
            retrieval_mode="qdrant",
            qdrant_candidate_reads=True,
        )
    )

    assert contained.effective_mode == "legacy"
    assert contained.containment_reason
    assert enabled.effective_mode == "qdrant"
    assert enabled.containment_reason is None


def test_vector_hash_tracks_authoritative_vector_and_policy_fields_only(tmp_path) -> None:
    svc = MemoryService(tmp_path / "hash.db", workspace_root=tmp_path)
    svc.init_db()
    claim = _seed(svc, "stable vector hash representation")

    assert claim_content_hash(claim) == claim_content_hash(replace(claim, citations=[]))
    assert claim_content_hash(claim) != claim_content_hash(replace(claim, status="stale"))
    assert claim_content_hash(claim) != claim_content_hash(replace(claim, scope="project:other"))
    assert QdrantBackend._claim_payload(claim) == {
        "claim_id": claim.id,
        "content_hash": claim_content_hash(claim),
    }


def test_authoritative_rehydration_rejects_every_untrusted_candidate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    svc = MemoryService(tmp_path / "governed-qdrant.db", workspace_root=tmp_path)
    svc.init_db()
    allowed = _seed(svc, "allowed semantic memory")
    stale_hash = _seed(svc, "changed after vector indexing")
    wrong_scope = _seed(svc, "foreign scope memory", scope="project:other")
    wrong_tenant = _seed(svc, "foreign tenant memory")
    private = replace(_seed(svc, "private agent memory"), visibility="private", source_agent="other")
    sensitive = _seed(svc, "credential AKIAIOSFODNN7EXAMPLE must remain hidden")
    archived = _seed(svc, "archived semantic memory")
    transition_claim(svc.store, archived.id, "archived", reason="fixture", event_type="transition")
    candidate = svc.ingest(
        "unconfirmed semantic memory",
        [CitationInput(source="test://candidate")],
        scope="project:allowed",
        source_agent="seed",
    )
    with svc.store.connect() as conn:
        conn.execute("UPDATE claims SET tenant_id = 'tenant-a' WHERE id != ?", (wrong_tenant.id,))
        conn.execute("UPDATE claims SET tenant_id = 'tenant-b' WHERE id = ?", (wrong_tenant.id,))
        conn.execute(
            "UPDATE claims SET visibility = 'private', source_agent = 'other' WHERE id = ?",
            (private.id,),
        )
        conn.commit()
    svc.tenant_id = "tenant-a"
    refreshed = {
        claim.id: svc.store.get_claim(claim.id, include_citations=True)
        for claim in (allowed, stale_hash, wrong_scope, wrong_tenant, private, sensitive, archived, candidate)
    }
    svc.qdrant = FakeCandidates(
        [
            _candidate(refreshed[allowed.id], score=0.99),
            _candidate(refreshed[stale_hash.id], content_hash="0" * 64),
            _candidate(refreshed[wrong_scope.id]),
            _candidate(refreshed[wrong_tenant.id]),
            _candidate(refreshed[private.id]),
            _candidate(refreshed[sensitive.id]),
            _candidate(refreshed[archived.id]),
            _candidate(refreshed[candidate.id]),
            QdrantCandidate(claim_id=999_999, content_hash="a" * 64, score=1.0),
        ]
    )

    result = svc.retrieve(
        RetrievalRequest(
            query_text="semantic memory",
            retrieval_mode="qdrant",
            qdrant_candidate_reads=True,
            scope_allowlist=("project:allowed",),
            requesting_agent="reader",
            limit=2,
        )
    )

    assert [row["claim"].id for row in result.rows] == [allowed.id]
    assert result.rows[0]["vector_score"] == 0.99
    assert svc.qdrant.limits == [20]


def test_backend_returns_only_validated_id_hash_candidates() -> None:
    backend = object.__new__(QdrantBackend)
    backend.qdrant_url = "https://qdrant.test"
    backend.collection = "claims"
    backend._qdrant_client = MagicMock()
    backend._ollama_client = MagicMock()
    backend._embed = lambda _text: [0.1] * EMBEDDING_DIMS
    response = MagicMock()
    response.json.return_value = {
        "result": [
            {"score": 0.8, "payload": {"claim_id": 7, "content_hash": "a" * 64, "claim_text": "hostile"}},
            {"score": 1.0, "payload": {"claim_id": True, "content_hash": "b" * 64}},
            {"score": 0.7, "payload": {"claim_id": 8, "content_hash": "short"}},
            {"score": "nan", "payload": {"claim_id": 9, "content_hash": "c" * 64}},
            {"score": 0.6, "payload": None},
        ]
    }
    backend._qdrant_client.post.return_value = response

    assert backend.search_candidates("query", limit=5) == [
        QdrantCandidate(claim_id=7, content_hash="a" * 64, score=0.8)
    ]
    body = backend._qdrant_client.post.call_args.kwargs["json"]
    assert body["with_payload"] == ["claim_id", "content_hash"]


def test_qdrant_outbox_is_bounded_and_replayable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_QDRANT_OUTBOX_DIR", str(tmp_path / "outbox"))
    svc = MemoryService(tmp_path / "outbox.db", workspace_root=tmp_path)
    svc.init_db()
    claim = _seed(svc, "replay this authoritative claim")

    assert qdrant_outbox.enqueue(svc.store.db_path, "upsert", claim.id, "0" * 64, max_entries=2)
    assert qdrant_outbox.enqueue(svc.store.db_path, "delete", 999, None, max_entries=2)
    assert not qdrant_outbox.enqueue(svc.store.db_path, "delete", 1000, None, max_entries=2)

    backend = MagicMock()
    backend.upsert_claim.return_value = True
    backend.delete_claim.return_value = True
    result = qdrant_outbox.replay(svc.store.db_path, svc.store, backend, max_operations=10)

    assert result == {"attempted": 2, "completed": 2, "remaining": 0}
    backend.upsert_claim.assert_called_once()
    backend.delete_claim.assert_called_once_with(999)
    assert qdrant_outbox.pending(svc.store.db_path) == []


def test_qdrant_outbox_retains_operations_when_backend_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_QDRANT_OUTBOX_DIR", str(tmp_path / "outbox"))
    svc = MemoryService(tmp_path / "retry.db", workspace_root=tmp_path)
    svc.init_db()
    claim = _seed(svc, "retry after transient qdrant exception")
    assert qdrant_outbox.enqueue(
        svc.store.db_path,
        "upsert",
        claim.id,
        claim_content_hash(claim),
    )
    backend = MagicMock()
    backend.upsert_claim.side_effect = RuntimeError("transient")

    result = qdrant_outbox.replay(svc.store.db_path, svc.store, backend)

    assert result == {"attempted": 1, "completed": 0, "remaining": 1}
    assert len(qdrant_outbox.pending(svc.store.db_path)) == 1


def test_equal_counts_still_reconcile_stale_missing_and_orphan_points(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MEMORYMASTER_QDRANT_OUTBOX_DIR", str(tmp_path / "outbox"))
    svc = MemoryService(tmp_path / "exact.db", workspace_root=tmp_path)
    svc.init_db()
    first = _seed(svc, "first exact reconciliation claim")
    second = _seed(svc, "second exact reconciliation claim")

    class ExactBackend:
        def __init__(self) -> None:
            self.upserted: list[int] = []
            self.deleted: list[int] = []

        def count_points(self):
            return 2

        def list_point_refs(self):
            return [
                QdrantCandidate(first.id, "0" * 64, 0.0),
                QdrantCandidate(999_999, "f" * 64, 0.0),
            ]

        def list_point_claim_ids(self):
            return [first.id, 999_999]

        def upsert_claim(self, claim):
            self.upserted.append(claim.id)
            return True

        def delete_claim(self, claim_id):
            self.deleted.append(claim_id)
            return True

        def sync_all(self, _store):
            pytest.fail("equal cardinality must not trigger full re-embedding")

    backend = ExactBackend()
    result = qdrant_reconcile.run(svc.store, backend, force=True, threshold=10)

    assert result["drift"] == 0
    assert set(backend.upserted) == {first.id, second.id}
    assert backend.deleted == [999_999]
    assert result["exact_reconcile"] == {"checked": 2, "upserted": 2, "deleted": 1}


def test_failed_service_sync_enters_metadata_only_outbox(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_QDRANT_OUTBOX_DIR", str(tmp_path / "outbox"))
    svc = MemoryService(tmp_path / "sync.db", workspace_root=tmp_path)
    svc.init_db()
    claim = _seed(svc, "failed vector sync is replayable")
    svc.qdrant = MagicMock()
    svc.qdrant.upsert_claim.return_value = False

    svc._qdrant_sync(claim)

    entries = qdrant_outbox.pending(svc.store.db_path)
    assert entries == [{
        "op": "upsert",
        "claim_id": claim.id,
        "content_hash": claim_content_hash(claim),
    }]
    assert "text" not in entries[0]


def test_mcp_semantic_profile_requires_explicit_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    monkeypatch.setenv("MEMORYMASTER_QDRANT_GOVERNED_READS", "1")
    svc = MemoryService(tmp_path / "mcp.db", workspace_root=tmp_path)
    svc.init_db()
    claim = _seed(svc, "governed semantic MCP result")
    fake = FakeCandidates([_candidate(claim)])
    svc.qdrant = fake
    monkeypatch.setattr(mcp_server, "_service", lambda *_args, **_kwargs: svc)

    result = mcp_server.query_memory(
        query="governed semantic MCP result",
        db=str(svc.store.db_path),
        workspace=str(tmp_path),
        retrieval_mode="qdrant",
        scope_allowlist="project:allowed",
    )

    assert {item["id"] for item in result["claims"]} == {claim.id}
    assert result["retrieval_mode"] == "qdrant"
    assert "containment_reason" not in result
    assert fake.limits == [80]
