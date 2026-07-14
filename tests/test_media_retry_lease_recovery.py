from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib
from pathlib import Path
import sqlite3

from memorymaster.core.service import MemoryService


def _queued_service(tmp_path: Path) -> tuple[MemoryService, int]:
    service = MemoryService(tmp_path / "lease.db", workspace_root=tmp_path)
    service.init_db()
    source = service.upsert_external_source(source_type="whatsapp", display_name="lease-test")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="media-1",
        item_type="audio",
        payload_json={"media_path": "media/one.ogg"},
    )
    queued = service.enqueue_media_retry(source_item_id=item.id, media_key="media/one.ogg")
    return service, queued.id


def test_expired_worker_lease_is_reclaimed_once(tmp_path: Path) -> None:
    service, retry_id = _queued_service(tmp_path)
    claimed = service.claim_pending_media_retries(limit=1, lease_owner="worker-a", lease_seconds=30)
    assert claimed[0].lease_owner == "worker-a"
    assert claimed[0].lease_expires_at

    with service.store.connect() as connection:
        connection.execute(
            "UPDATE media_retry_queue SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", retry_id),
        )
        connection.commit()

    reclaimed = service.claim_pending_media_retries(limit=1, lease_owner="worker-b", lease_seconds=30)
    assert [row.id for row in reclaimed] == [retry_id]
    assert reclaimed[0].lease_owner == "worker-b"
    assert reclaimed[0].attempt_count == 2
    details = [event.details for event in service.list_events(limit=50)]
    assert details.count("media_retry_lease_expired") == 1


def test_unexpired_worker_lease_is_not_stolen(tmp_path: Path) -> None:
    service, _ = _queued_service(tmp_path)
    service.claim_pending_media_retries(limit=1, lease_owner="worker-a", lease_seconds=300)

    assert service.claim_pending_media_retries(limit=1, lease_owner="worker-b", lease_seconds=300) == []


def test_concurrent_reclaim_has_one_winner(tmp_path: Path) -> None:
    service, retry_id = _queued_service(tmp_path)
    service.claim_pending_media_retries(limit=1, lease_owner="dead-worker", lease_seconds=30)
    with service.store.connect() as connection:
        connection.execute(
            "UPDATE media_retry_queue SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", retry_id),
        )
        connection.commit()

    def claim(owner: str):
        contender = MemoryService(service.store.db_path, workspace_root=tmp_path)
        return contender.claim_pending_media_retries(limit=1, lease_owner=owner, lease_seconds=300)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-b", "worker-c")))

    winners = [rows[0] for rows in results if rows]
    assert len(winners) == 1
    assert winners[0].attempt_count == 2


def test_lease_migration_upgrades_a_stale_queue() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE media_retry_queue (id INTEGER PRIMARY KEY, status TEXT)")
    migration = importlib.import_module("memorymaster.stores.migrations.0016_media_retry_leases")

    migration.apply_sqlite(connection)

    columns = {row[1] for row in connection.execute("PRAGMA table_info(media_retry_queue)")}
    assert {"lease_owner", "lease_expires_at"} <= columns
    indexes = {row[1] for row in connection.execute("PRAGMA index_list(media_retry_queue)")}
    assert "idx_media_retry_lease_expiry" in indexes


def test_baseline_schemas_keep_sqlite_postgres_lease_parity() -> None:
    root = Path(__file__).resolve().parents[1]
    sqlite_schema = (root / "memorymaster/schema.sql").read_text(encoding="utf-8")
    postgres_schema = (root / "memorymaster/schema_postgres.sql").read_text(encoding="utf-8")

    for field in ("lease_owner", "lease_expires_at", "idx_media_retry_lease_expiry"):
        assert field in sqlite_schema
        assert field in postgres_schema
