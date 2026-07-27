from __future__ import annotations

import hashlib
import importlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from memorymaster.capture.repository import CaptureRepository
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


def _service(tmp_path) -> MemoryService:
    service = MemoryService(tmp_path / "capture.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _source(service: MemoryService, *, external_id: str = "source-1"):
    external = service.upsert_external_source(
        source_type="direct", display_name="capture-test"
    )
    return service.upsert_source_item(
        source_id=external.id,
        source_item_id=external_id,
        item_type="text",
        text="Project Alder uses SQLite.",
        content_hash=hashlib.sha256(b"Project Alder uses SQLite.").hexdigest(),
        sensitivity="none",
    )


def test_migration_creates_additive_schema_and_is_idempotent(tmp_path) -> None:
    service = _service(tmp_path)
    service.init_db()

    with service.store.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name IN (
                       'claim_evidence_links','capture_jobs','entity_edge_supports')"""
            )
        }
        source_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(source_items)")
        }
        evidence_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(evidence_items)")
        }
        versions = conn.execute(
            "SELECT COUNT(*) FROM schema_versions WHERE version=17"
        ).fetchone()[0]

    assert tables == {
        "claim_evidence_links",
        "capture_jobs",
        "entity_edge_supports",
    }
    assert {"retired_at", "retirement_reason"} <= source_columns
    assert "content_hash" in evidence_columns
    assert versions == 1


def test_queue_and_lease_are_replay_safe(tmp_path) -> None:
    service = _service(tmp_path)
    source = _source(service)
    repo = CaptureRepository(service.store)
    digest = hashlib.sha256(b"Project Alder uses SQLite.").hexdigest()

    first, created_first = repo.queue_job(
        source_item_id=source.id, content_hash=digest, stage="extract_claims"
    )
    second, created_second = repo.queue_job(
        source_item_id=source.id, content_hash=digest, stage="extract_claims"
    )
    leased = repo.lease_jobs(owner="test-worker", limit=10)
    completed = repo.finish_job(leased[0].id, status="completed")

    assert created_first is True
    assert created_second is False
    assert first.id == second.id == leased[0].id
    assert leased[0].attempts == 1
    assert completed.status == "completed"
    assert repo.lease_jobs(owner="test-worker") == []


def test_expired_leases_retry_and_block_after_five_attempts(tmp_path) -> None:
    service = _service(tmp_path)
    source = _source(service)
    repo = CaptureRepository(service.store)
    digest = hashlib.sha256(b"lease-test").hexdigest()
    job, _ = repo.queue_job(
        source_item_id=source.id, content_hash=digest, stage="extract_text"
    )

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with service.store.connect() as conn:
        conn.execute(
            """UPDATE capture_jobs SET status='leased', attempts=4,
               lease_owner='crashed', lease_expires_at=? WHERE id=?""",
            (past, job.id),
        )
        conn.commit()

    fifth = repo.lease_jobs(owner="replacement", lease_seconds=30)[0]
    assert fifth.attempts == 5
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE capture_jobs SET lease_expires_at=? WHERE id=?",
            (past, job.id),
        )
        conn.commit()

    assert repo.lease_jobs(owner="replacement") == []
    with service.store.connect() as conn:
        row = conn.execute(
            "SELECT status, error_code FROM capture_jobs WHERE id=?", (job.id,)
        ).fetchone()
    assert tuple(row) == ("blocked", "attempts_exhausted")


def test_retry_backoff_is_bounded_and_sensitive_errors_are_sanitized(tmp_path) -> None:
    service = _service(tmp_path)
    source = _source(service)
    repo = CaptureRepository(service.store)
    digest = hashlib.sha256(b"retry-test").hexdigest()
    job, _ = repo.queue_job(
        source_item_id=source.id, content_hash=digest, stage="extract_claims"
    )
    leased = repo.lease_jobs(owner="worker")[0]
    retry = repo.finish_job(
        leased.id,
        status="retryable",
        error_code="provider_timeout",
        error_detail="OPENAI_API_KEY=sk-proj-FAKEcaptureStorage1234567890ABCDE",
    )

    assert retry.status == "retryable"
    assert retry.next_attempt_at is not None
    assert "FAKEcaptureStorage" not in (retry.error_detail or "")


def test_exact_backfill_links_evidence_and_edge_support_without_guessing(tmp_path) -> None:
    service = _service(tmp_path)
    source = _source(service)
    evidence = service.add_evidence_item(
        source_item_id=source.id,
        evidence_type="text",
        text="Project Alder uses SQLite.",
        sensitivity="none",
    )
    claim = service.ingest(
        "Project Alder uses SQLite.",
        [CitationInput(source="atlas://source/1", locator=f"evidence:{evidence.id}")],
        subject="Project Alder",
        predicate="uses",
        object_value="SQLite",
        scope="project:test",
        source_agent="capture-test",
    )
    with service.store.connect() as conn:
        stamp = datetime.now(timezone.utc).isoformat()
        source_row = conn.execute(
            "SELECT id FROM entities WHERE canonical_name='Project Alder'"
        ).fetchone()
        source_entity = int(source_row[0])
        target_row = conn.execute(
            "SELECT id FROM entities WHERE canonical_name='SQLite'"
        ).fetchone()
        if target_row is None:
            target_entity = conn.execute(
                """INSERT INTO entities
                   (canonical_name, entity_type, scope, created_at, updated_at)
                   VALUES ('SQLite','system','project:test',?,?)""",
                (stamp, stamp),
            ).lastrowid
        else:
            target_entity = int(target_row[0])
        conn.execute(
            """INSERT INTO entity_edges
               (source_id,target_id,relation,weight,claim_id,created_at,last_reinforced_at)
               VALUES (?,?,?,1.0,?,?,?)""",
            (source_entity, target_entity, "uses", claim.id, stamp, stamp),
        )
        conn.execute(
            """INSERT INTO citations (claim_id,source,locator,excerpt,created_at)
               VALUES (?, 'legacy', 'evidence:not-an-id', NULL, ?)""",
            (claim.id, stamp),
        )
        conn.execute("DELETE FROM claim_evidence_links")
        conn.execute("DELETE FROM entity_edge_supports")
        migration = importlib.import_module(
            "memorymaster.stores.migrations.0017_governed_capture_lineage"
        )
        report = migration.backfill_lineage(conn, backend="sqlite", apply=True)
        conn.commit()
        links = conn.execute("SELECT claim_id,evidence_item_id FROM claim_evidence_links").fetchall()
        supports = conn.execute(
            "SELECT supporting_claim_id,scope,ontology_version FROM entity_edge_supports"
        ).fetchall()

    assert report["exact_evidence_links"] == 1
    assert report["ambiguous_locators"] == 1
    assert report["exact_edge_supports"] == 1
    assert [tuple(row) for row in links] == [(claim.id, evidence.id)]
    assert [tuple(row) for row in supports] == [
        (claim.id, "project:test", "legacy-v0")
    ]


def test_lineage_and_support_uniqueness_prevent_reinforcement(tmp_path) -> None:
    service = _service(tmp_path)
    source = _source(service)
    evidence = service.add_evidence_item(
        source_item_id=source.id, evidence_type="text", text="Evidence"
    )
    claim = service.ingest(
        "A uses B.",
        [CitationInput(source="test", locator=f"evidence:{evidence.id}")],
        subject="A",
        predicate="uses",
        object_value="B",
        scope="project:test",
        source_agent="capture-test",
    )
    repo = CaptureRepository(service.store)
    first_link = repo.link_claim_evidence(
        claim_id=claim.id, evidence_item_id=evidence.id
    )
    second_link = repo.link_claim_evidence(
        claim_id=claim.id, evidence_item_id=evidence.id
    )
    stamp = datetime.now(timezone.utc).isoformat()
    with service.store.connect() as conn:
        a_id = int(
            conn.execute("SELECT id FROM entities WHERE canonical_name='A'").fetchone()[0]
        )
        b_row = conn.execute(
            "SELECT id FROM entities WHERE canonical_name='B'"
        ).fetchone()
        if b_row is None:
            b_id = conn.execute(
                """INSERT INTO entities
                   (canonical_name,entity_type,scope,created_at,updated_at)
                   VALUES ('B','concept','project:test',?,?)""",
                (stamp, stamp),
            ).lastrowid
        else:
            b_id = int(b_row[0])
        conn.commit()
    first_support = repo.add_edge_support(
        source_entity_id=a_id,
        target_entity_id=b_id,
        relation="uses",
        supporting_claim_id=claim.id,
        scope="project:test",
        ontology_version="personal-v1",
    )
    second_support = repo.add_edge_support(
        source_entity_id=a_id,
        target_entity_id=b_id,
        relation="uses",
        supporting_claim_id=claim.id,
        scope="project:test",
        ontology_version="personal-v1",
    )

    assert first_link[1] is True and second_link[1] is False
    assert first_support[1] is True and second_support[1] is False


def test_retirement_is_logical_and_cancels_pending_jobs(tmp_path) -> None:
    service = _service(tmp_path)
    source = _source(service)
    repo = CaptureRepository(service.store)
    digest = hashlib.sha256(b"retire").hexdigest()
    repo.queue_job(
        source_item_id=source.id, content_hash=digest, stage="extract_claims"
    )

    assert repo.retire_source(source.id, reason="operator request") is True
    assert repo.retire_source(source.id, reason="operator request") is False
    with service.store.connect() as conn:
        source_row = conn.execute(
            "SELECT retired_at,retirement_reason FROM source_items WHERE id=?",
            (source.id,),
        ).fetchone()
        job_status = conn.execute(
            "SELECT status FROM capture_jobs WHERE source_item_id=?", (source.id,)
        ).fetchone()[0]
        fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()

    assert source_row[0] is not None
    assert source_row[1] == "operator request"
    assert job_status == "cancelled"
    assert fk_issues == []


def test_disposable_restore_keeps_old_reads_compatible(tmp_path) -> None:
    service = _service(tmp_path)
    source = _source(service)
    evidence = service.add_evidence_item(
        source_item_id=source.id, evidence_type="text", text="restorable"
    )
    restored = tmp_path / "restored.db"
    with service.store.connect() as source_conn, sqlite3.connect(restored) as target_conn:
        source_conn.backup(target_conn)

    restored_service = MemoryService(restored, workspace_root=tmp_path)
    restored_service.init_db()
    restored_source = restored_service.get_source_item_by_id(source.id)
    restored_evidence = restored_service.list_evidence_items(
        source_item_id=source.id
    )

    assert restored_source is not None
    assert restored_source.text == source.text
    assert [item.id for item in restored_evidence] == [evidence.id]


def test_postgres_migration_declares_parity_tables_and_columns() -> None:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0017_governed_capture_lineage"
    )

    class Cursor:
        def __init__(self):
            self.rows = []
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, _params=()):
            self.statements.append(" ".join(statement.split()))
            if "information_schema.tables" in statement:
                self.rows = [{"count": 4}]
            elif "SELECT claim_id, locator FROM citations" in statement:
                self.rows = []
            elif "FROM entity_edges WHERE claim_id IS NOT NULL" in statement:
                self.rows = []
            else:
                self.rows = []
            return self

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return list(self.rows)

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()
            self.commits = 0

        def cursor(self):
            return self.cursor_instance

        def commit(self):
            self.commits += 1

    connection = Connection()
    migration.apply_postgres(connection)
    ddl = "\n".join(connection.cursor_instance.statements)

    assert "ADD COLUMN IF NOT EXISTS content_hash" in ddl
    assert "ADD COLUMN IF NOT EXISTS retired_at" in ddl
    assert "CREATE TABLE IF NOT EXISTS claim_evidence_links" in ddl
    assert "CREATE TABLE IF NOT EXISTS capture_jobs" in ddl
    assert "CREATE TABLE IF NOT EXISTS entity_edge_supports" in ddl
    assert connection.commits == 1


@pytest.mark.parametrize("stage", ["unknown", "", "extract-video"])
def test_unknown_capture_stage_fails_closed(tmp_path, stage) -> None:
    service = _service(tmp_path)
    source = _source(service)
    repo = CaptureRepository(service.store)
    with pytest.raises(ValueError):
        repo.queue_job(
            source_item_id=source.id,
            content_hash=hashlib.sha256(b"x").hexdigest(),
            stage=stage,
        )
