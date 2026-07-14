from __future__ import annotations

from memorymaster.core.models import Claim
from memorymaster.recall.qdrant_backend import QdrantBackend


def _claim(claim_id: int) -> Claim:
    return Claim(
        id=claim_id,
        text=f"claim {claim_id}",
        idempotency_key=None,
        normalized_text=None,
        claim_type="fact",
        subject=None,
        predicate=None,
        object_value=None,
        scope="project:test",
        volatility="medium",
        status="confirmed",
        confidence=0.9,
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        last_validated_at=None,
        archived_at=None,
    )


class _PagedStore:
    tenant_id = "tenant-a"

    def __init__(self, claims: list[Claim], cursor: int = 0) -> None:
        self.claims = claims
        self.cursor = cursor
        self.pages: list[int] = []
        self.saved: list[int] = []

    def get_qdrant_sync_cursor(self, stream_key: str) -> int:
        assert "tenant-a" in stream_key
        return self.cursor

    def list_qdrant_sync_page(self, *, after_id: int, limit: int) -> list[Claim]:
        self.pages.append(after_id)
        return [claim for claim in self.claims if claim.id > after_id][:limit]

    def set_qdrant_sync_cursor(self, stream_key: str, last_claim_id: int) -> None:
        assert "tenant-a" in stream_key
        self.cursor = last_claim_id
        self.saved.append(last_claim_id)


def _backend() -> QdrantBackend:
    backend = object.__new__(QdrantBackend)
    backend.collection = "test-collection"
    backend.embed_model = "test-model"
    backend.ensure_collection = lambda: None
    backend._embed = lambda text: [float(len(text))]
    return backend


def test_sync_all_pages_every_authoritative_claim_and_resets_cursor() -> None:
    store = _PagedStore([_claim(i) for i in range(1, 8)])
    backend = _backend()
    batches: list[list[int]] = []
    backend._batch_upsert = lambda points: batches.append(
        [int(point["payload"]["claim_id"]) for point in points]
    ) or True

    result = backend.sync_all(store, batch_size=3)

    assert result == {"total": 7, "synced": 7, "skipped": 0, "errors": 0}
    assert batches == [[1, 2, 3], [4, 5, 6], [7]]
    assert store.pages == [0, 3, 6, 7]
    assert store.saved == [3, 6, 7, 0]


def test_sync_all_failure_preserves_last_completed_page_for_replay() -> None:
    store = _PagedStore([_claim(i) for i in range(1, 6)])
    backend = _backend()
    outcomes = iter([True, False])
    backend._batch_upsert = lambda _points: next(outcomes)

    result = backend.sync_all(store, batch_size=2)

    assert result["synced"] == 2
    assert result["errors"] == 2
    assert store.cursor == 2
    assert store.saved == [2]


def test_sync_cursor_is_durable_and_authority_scoped(tmp_path) -> None:
    from memorymaster.stores.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "cursor.db")
    store.init_db()
    key_a = "collection:model:tenant-a"
    key_b = "collection:model:tenant-b"
    store.set_qdrant_sync_cursor(key_a, 41)
    store.set_qdrant_sync_cursor(key_b, 9)

    reopened = SQLiteStore(store.db_path)
    assert reopened.get_qdrant_sync_cursor(key_a) == 41
    assert reopened.get_qdrant_sync_cursor(key_b) == 9
